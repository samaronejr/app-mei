"""One hold/eligibility decision and explicit, versioned hold commands."""

import calendar
import hashlib
import os
import posixpath
import re
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import IO, Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.files.storage import FileSystemStorage, Storage, default_storage
from django.db import connection, transaction
from django.db.models import QuerySet
from django.http import Http404
from storages.utils import clean_name

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.clients.maintenance import load_client
from apps.core.tenancy import tenant_context
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence import DocumentIntake
from apps.cycles.export_records import EvidenceExport
from apps.cycles.retention_records import ContentPurgeClaim, LegalHold
from apps.cycles.services.cycle import (
    check_version,
    require_actor,
    require_enabled,
    tenant_id,
)
from apps.integrations.call_models import NO_BYTES_STATES, ProviderCall
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FiscalSituationReport,
)
from apps.tenants.models import Tenant

MAX_HOLD_REASON = 2000
FISCAL_TIMEZONE: Final = ZoneInfo("America/Sao_Paulo")


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """Unknown classification and active holds fail closed."""

    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RetentionAnchor:
    """One verified reference that places a retained copy in a fiscal year.

    `basis` names where the date comes from (a linked cycle, an obligation, a report
    emission...), `on` is that date and `source_id` the row that holds it.
    """

    basis: str
    on: date
    source_id: UUID


SINGLE_COPY_STORAGES: Final = (FileSystemStorage,)


def single_copy_storage(storage: Storage | None = None) -> bool:
    """Tell whether a key delete removes the only stored copy (an allow-list).

    A versioned bucket keeps noncurrent versions that a client hold cannot
    protect, and an unrecognized backend may do the same: neither is supported
    for disposing of federal content.
    """
    chosen = default_storage if storage is None else storage
    return chosen.__class__ in SINGLE_COPY_STORAGES


def local_layout_refusal(keys: Iterable[str], storage: Storage | None = None) -> str:
    """On local storage, why these keys are not proved to name files of their own.

    A key string cannot capture the physical file. The root, taken at its real
    path, must hold no mount point (a bind mount shows one file at two names), no
    symlink, no directory that cannot be listed and no directory that ignores
    case; each key, at the path the backend addresses, must resolve inside it and,
    if it exists, be a regular file with one link (AXRM-051). Paths are compared
    as bytes, so no name's encoding can break a match. Then two keys name one
    object exactly when their `physical_identity` is the same. Nothing is opened.
    Empty when proved, and on any backend that is not a local filesystem.
    """
    chosen = default_storage if storage is None else storage
    if not isinstance(chosen, FileSystemStorage):
        return ""
    root = real_root(chosen)
    refusal = _mount_refusal(root) or _walk_refusal(root)
    if refusal:
        return refusal
    return next(
        (refusal for key in keys if (refusal := _key_refusal(chosen, root, key))), ""
    )


def real_root(storage: FileSystemStorage) -> Path:
    """Return the storage root at its real path (a symlinked root resolved)."""
    return Path(os.path.realpath(storage.location))


def _inside(path: Path | bytes, root: Path) -> bool:
    """Whether `path` is `root` or under it, compared as bytes."""
    raw = path if isinstance(path, bytes) else os.fsencode(path)
    base = os.fsencode(root).rstrip(b"/")
    return raw == base or raw.startswith(base + b"/")


_MOUNT_ESCAPE: Final = re.compile(rb"\\([0-7]{3})")


def _mount_table() -> bytes:
    """Return the kernel's mount table for this process, as bytes."""
    return Path("/proc/self/mountinfo").read_bytes()


def _mount_points(table: bytes) -> list[bytes]:
    """Each mount point (fifth field), its octal escapes decoded on the bytes."""
    return [_mount_point(line) for line in table.split(b"\n") if line]


def _mount_point(line: bytes) -> bytes:
    """Return one mountinfo line's mount point, split as the kernel writes it.

    Fields are separated by single spaces only; the path fields escape space, tab,
    newline and backslash, so every other byte (vertical tab, form feed, carriage
    return, a no-break space) is part of a name. The optional fields end at a
    lone `-`.
    """
    fields = line.split(b" ")
    if b"-" not in fields[6:]:
        msg = "mountinfo line without its optional-fields separator"
        raise ValueError(msg)
    return _MOUNT_ESCAPE.sub(lambda match: bytes([int(match[1], 8)]), fields[4])


def _mount_refusal(root: Path) -> str:
    """Refuse a root with any mount point strictly under it, or an unreadable table."""
    try:
        points = _mount_points(_mount_table())
    except (OSError, IndexError, ValueError):
        return "armazenamento_nao_inspecionavel"
    base = os.fsencode(root).rstrip(b"/")
    if any(point != base and _inside(point, root) for point in points):
        return "armazenamento_com_montagem"
    return ""


def _walk_refusal(root: Path) -> str:
    """Walk the root once: symlinks, unlisted directories, case-blind directories."""
    unreadable: list[OSError] = []
    for parent, directories, files in os.walk(root, onerror=unreadable.append):
        names = (*directories, *files)
        if any((Path(parent) / name).is_symlink() for name in names):
            return "armazenamento_com_atalho"
        refusal = _case_refusal(Path(parent), names)
        if refusal:
            return refusal
    if unreadable:
        return "armazenamento_nao_inspecionavel"  # an unlisted directory proves nothing
    return ""


def _case_refusal(directory: Path, names: tuple[str, ...]) -> str:
    """Probe one entry for a directory that ignores case.

    Another case of the entry's name opening it means the directory ignores case;
    a probe that cannot be answered proves nothing.
    """
    name = next((n for n in names if n.lower() != n.upper()), None)
    if name is None:
        return ""
    other = directory / (name.lower() if name != name.lower() else name.upper())
    try:
        same = (directory / name).samefile(other)
    except FileNotFoundError:
        return ""  # no such entry: names differing in case are distinct here
    except OSError:
        return "armazenamento_nao_inspecionavel"
    return "armazenamento_sem_distincao_de_caixa" if same else ""


def _addressed(storage: FileSystemStorage, key: str) -> Path | None:
    """Return the real path the backend addresses for a key (None outside it)."""
    try:
        return Path(os.path.realpath(storage.path(key)))
    except SuspiciousFileOperation:
        if Path(key).is_absolute():
            # A direct spelling the backend will not address (the real root of a
            # symlinked location, say) still names whatever file it reaches.
            return Path(os.path.realpath(key))
        return None


def physical_identity(key: str, storage: Storage) -> bytes:
    """Return the identity of the object a key names in `storage`, as bytes.

    An existing local regular file is its inode, `(st_dev, st_ino)` from `lstat`
    at the path the backend addresses: a bind-mounted view, a hard link, a
    symlinked ancestor or any other alias then compares equal by construction. A
    local object that does not exist yet is named by that real path, and a remote
    one by its lexical name (AXRM-051).
    """
    if not isinstance(storage, FileSystemStorage):
        return _remote_identity(key, storage)
    path = _resolved(storage, key)
    try:
        status = _lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return b"path:" + os.fsencode(path)  # no such object can exist there yet
    if not stat.S_ISREG(status.st_mode):
        return b"path:" + os.fsencode(path)
    return b"inode:%d:%d" % (status.st_dev, status.st_ino)


def _remote_identity(key: str, storage: Storage) -> bytes:
    """Return the object a remote backend itself resolves a key to.

    A backend that resolves names (django-storages' S3Storage strips a leading `/`
    and folds `..` and `.` through `_normalize_name(clean_name(...))`) is asked
    for its own resolution, so every spelling that names one object compares
    equal. A name it refuses raises, and its identity is then unknown. A backend
    without such a resolution falls back to the lexical form (AXRM-051).
    """
    return b"remote:" + _remote_name(key, storage).encode("utf-8", "surrogateescape")


def _remote_name(key: str, storage: Storage) -> str:
    normalize = getattr(storage, "_normalize_name", None)
    if normalize is None:
        return posixpath.normpath(key.replace("\\", "/"))
    resolved: str = normalize(clean_name(key))
    return resolved


def resolves_under(key: str, prefix: str, storage: Storage) -> bool:
    """Whether a key resolves strictly inside a prefix (the resolved name)."""
    name = resolved_name(key, storage)
    return name is not None and name.startswith(prefix) and len(name) > len(prefix)


def resolves_to_document_final(key: str, tenant: object, storage: Storage) -> bool:
    """Whether a key resolves to one of the names documents are written with.

    A random single-segment key (intake), a fresh `originals/` key (remote
    promotion) or this tenant's `qualification/` record; nothing else.
    """
    name = resolved_name(key, storage)
    if name is None:
        return False
    return bool(
        re.fullmatch(r"[A-Za-z0-9_-]+", name)
        or re.fullmatch(r"originals/[^/]+", name)
        or re.fullmatch(rf"qualification/{re.escape(str(tenant))}/[^/]+", name)
    )


def resolved_name(key: str, storage: Storage) -> str | None:
    """Return the root-relative name the backend resolves a key to, or None.

    Locally the real path the backend addresses, relative to the real root (None
    when it leaves the root); remotely the backend's own resolution. Prefix and
    shape checks read this name, never the spelling (AXRM-051).
    """
    try:
        if not isinstance(storage, FileSystemStorage):
            return _remote_name(key, storage)
        path, root = _resolved(storage, key), real_root(storage)
        return path.relative_to(root).as_posix() if _inside(path, root) else None
    except Exception:  # noqa: BLE001 - a name that cannot be resolved is refused
        return None


def _resolved(storage: FileSystemStorage, key: str) -> Path:
    """Return the real path the backend addresses for a key.

    A spelling the backend refuses is resolved directly, for identification only.
    """
    path = _addressed(storage, key)
    return path or Path(os.path.realpath(Path(storage.location) / key))


def _path_identity(key: str, storage: Storage) -> bytes:
    """Return the name a lock takes for a key: the same before and after it exists."""
    if not isinstance(storage, FileSystemStorage):
        return _remote_identity(key, storage)
    return b"path:" + os.fsencode(_resolved(storage, key))


def rows_storage() -> Storage:
    """Return the storage every stored row's key belongs to: the default one."""
    return default_storage


def _is_rows_storage(storage: Storage) -> bool:
    """Whether `storage` is the rows' own storage: one storage per decision."""
    return storage is rows_storage()


def _key_refusal(storage: FileSystemStorage, root: Path, key: str) -> str:
    """Why one key is not proved to name a file of its own inside `root`, or ``."""
    refusal = _naming_refusal(storage, root, key)
    if refusal:
        return refusal
    path = Path(os.path.realpath(storage.path(key)))
    try:
        status = path.lstat()
    except FileNotFoundError:
        return ""
    except (OSError, RuntimeError):
        return "armazenamento_nao_inspecionavel"  # never an uncaught stop
    if not stat.S_ISREG(status.st_mode):
        return "arquivo_nao_regular"  # a FIFO, socket or device is never opened
    return "arquivo_com_vinculos" if status.st_nlink > 1 else ""


def _naming_refusal(storage: FileSystemStorage, root: Path, key: str) -> str:
    """Why a key does not name, inside `root`, a file this backend addresses."""
    if _identity_or_none(key, storage) is None:
        return "identidade_indeterminada"  # refused with its reason, never raised
    path = _addressed(storage, key)
    if path is None or not _inside(path, root):
        return "chave_fora_do_armazenamento"
    try:
        storage.path(key)
    except SuspiciousFileOperation:
        # A spelling the deleting backend will not address is never deletable,
        # even when it can be identified for ownership (AXRM-051).
        return "chave_nao_enderecavel"
    return ""


class NotRegularFileError(OSError):
    """A stored object that is not a regular file: never read."""


# One indirection each, so a test can record an open or stand in for a swap.
_lstat = os.lstat
_open = os.open


def open_stored(storage: Storage, key: str) -> IO[bytes]:
    """Open a stored object for reading; on local storage only a regular file.

    Every read of a stored object goes through here. Locally the name is checked
    with `lstat`, opened without following a link and without blocking, and the
    descriptor is checked again with `fstat` (same file, still regular), so a
    FIFO, socket, device or a swap between the checks is never read.
    """
    if not isinstance(storage, FileSystemStorage):
        return storage.open(key, "rb")
    path = storage.path(key)
    before = _lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise NotRegularFileError(path)
    descriptor = _open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    after = os.fstat(descriptor)
    if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
        before.st_dev,
        before.st_ino,
    ):
        os.close(descriptor)
        raise NotRegularFileError(path)
    return os.fdopen(descriptor, "rb")


def federal_content(document: Document) -> bool:
    """Tell whether SERPRO obtained these bytes or a SITFIS report holds them."""
    return (
        document.acquisition_origin == AcquisitionOrigin.FEDERAL_PROVIDER
        or FiscalSituationReport.objects.filter(document=document).exists()
    )


def client_federal_involvement(client_id: UUID) -> bool:
    """Tell whether any of a client's content may be federal, whatever its origin.

    True for a SERPRO original, a report holding an original, or any provider call
    that may have stored bytes: a staff-first original SERPRO reused carries no
    federal mark of its own, so a copy of the client's content is classified by
    this, not by the origin of its first copy.
    """
    return (
        Document.objects.filter(
            client_id=client_id, acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER
        ).exists()
        or FiscalSituationReport.objects.filter(
            client_id=client_id, document__isnull=False
        ).exists()
        or ProviderCall.objects.filter(client_id=client_id)
        .exclude(state__in=NO_BYTES_STATES)
        .exists()
    )


# Names that resolve to one object differ only in separators and `.` segments, so
# rows are matched with both removed: a coarser match only keeps more.
def _identity_or_none(key: str, storage: Storage) -> bytes | None:
    """Return `physical_identity`, or None when it cannot be computed; never raises."""
    try:
        return physical_identity(key, storage)
    except Exception:  # noqa: BLE001 - any failure means "unknown", never a stop
        return None


def _row_names(raw: str, identities: set[bytes]) -> bool:
    """Whether a stored row's key names one of these objects (or cannot be told)."""
    identity = _identity_or_none(raw, rows_storage())
    return identity is None or identity in identities


def _known_identities(keys: Iterable[str], storage: Storage) -> set[bytes] | None:
    """Return these keys' identities, or None when any of them is unknown."""
    identities = [_identity_or_none(key, storage) for key in keys]
    if any(identity is None for identity in identities):
        return None  # an unknown candidate matches every row, never none
    return {identity for identity in identities if identity is not None}


def _naming(
    model: type[Document] | type[DocumentIntake] | type[EvidenceExport],
    field: str,
    keys: Iterable[str],
    storage: Storage | None = None,
) -> QuerySet[Any]:
    """Return the rows of `model` whose key names one of these keys' objects.

    Every row is compared by physical identity, never pre-filtered by its
    spelling. A row whose identity cannot be computed counts as naming every key,
    and so does every row when a key's own identity is unknown or the keys belong
    to any storage but the rows' own (AXRM-051).
    """
    chosen = default_storage if storage is None else storage
    named = [key for key in keys if key]
    stored = model.objects.exclude(**{f"{field}__isnull": True}).exclude(**{field: ""})
    identities = _known_identities(named, chosen) if _is_rows_storage(chosen) else None
    if identities is None:
        return stored  # another storage, or an unknown key: every row counts
    matching = [
        pk for pk, raw in stored.values_list("pk", field) if _row_names(raw, identities)
    ]
    return model.objects.filter(pk__in=matching)


def storage_key_aliased(
    keys: Iterable[str],
    *,
    document_id: UUID | None = None,
    export_id: UUID | None = None,
    storage: Storage | None = None,
) -> bool:
    """Tell whether any row other than this one names one of these storage keys.

    Every row kind that names a key counts: an original's final key, an intake's
    quarantine copy and an evidence archive, of any client of the tenant, under
    any spelling that resolves to the same object.
    """
    named = [key for key in keys if key]
    if not named:
        return False
    finals = _naming(Document, "storage_key", named, storage)
    copies = _naming(DocumentIntake, "quarantine_key", named, storage)
    archives = _naming(EvidenceExport, "storage_key", named, storage)
    if document_id is not None:
        finals = finals.exclude(pk=document_id)
        copies = copies.exclude(document_id=document_id)
    if export_id is not None:
        archives = archives.exclude(pk=export_id)
    return finals.exists() or copies.exists() or archives.exists()


def foreign_storage_keys(
    tenant_id: UUID, keys: Iterable[str], storage: Storage | None = None
) -> set[str]:
    """Return which of these keys a row of another tenant names, under any spelling.

    Storage keys are global while rows are tenant-scoped, so every other tenant is
    read in its own context: an original's final key, an intake's quarantine copy
    or an evidence archive there keeps the key here.
    """
    named = sorted({key for key in keys if key})
    found: set[str] = set()
    if not named:
        return set()
    # PLATFORM_QUERY_OK: storage keys are global, so every other tenant is checked.
    others = Tenant.objects.exclude(pk=tenant_id).values_list("pk", flat=True)
    for other in list(others):
        with tenant_context(other):
            for model, field in (
                (Document, "storage_key"),
                (DocumentIntake, "quarantine_key"),
                (EvidenceExport, "storage_key"),
            ):
                found.update(
                    _naming(model, field, named, storage).values_list(field, flat=True)
                )
    chosen = default_storage if storage is None else storage
    if not _is_rows_storage(chosen):
        return set(named) if found else set()
    return {
        key
        for key in named
        for raw in found
        if (identities := _known_identities([key], chosen)) is None
        or _row_names(raw, identities)
    }


def ownership_refusal(
    tenant_id: UUID,
    keys: Iterable[str],
    *,
    document_id: UUID | None = None,
    export_id: UUID | None = None,
    storage: Storage | None = None,
) -> str:
    """Why these keys may not be deleted for this row, or ``: the one ownership check.

    Another row of the tenant, a row of any other tenant (both matched on the
    canonical key) or an unproved local layout keeps the bytes. The purge and the
    scanner's cleanup both decide through here, outside any tenant context, with
    the storage that will read or delete the bytes (AXRM-051).
    """
    named = [key for key in keys if key]
    if not named:
        return ""
    if not _is_rows_storage(default_storage if storage is None else storage):
        # Only the rows' own storage may read or delete what the rows name; any
        # other is never compared across, so nothing is deleted (AXRM-051).
        return "armazenamento_divergente"
    with tenant_context(tenant_id):
        shared = storage_key_aliased(
            named, document_id=document_id, export_id=export_id, storage=storage
        )
    if shared or foreign_storage_keys(tenant_id, named, storage):
        return "chave_compartilhada"
    return local_layout_refusal(named, storage)


def _key_lock_id(key: str, storage: Storage | None = None) -> int | None:
    # One lock per object, whatever spelling or storage root a caller holds.
    chosen = default_storage if storage is None else storage
    # Locks are named by path, which stays the same before and after the object
    # exists. A name that cannot be computed is None: never a substitute lock,
    # which another holder of the same object would not share (AXRM-051).
    try:
        name = _path_identity(key, chosen)
    except Exception:  # noqa: BLE001 - the caller refuses, keeping the bytes
        return None
    digest = hashlib.sha256(b"axorim-storage-key:" + name).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def storage_keys_locked(
    keys: Iterable[str], *, wait: bool, storage: Storage | None = None
) -> Iterator[bool]:
    """Hold a session advisory lock per storage key; yield whether all were taken.

    Every writer of an original's final key and every disposal of it takes these
    locks, outside any transaction, so verifying the bytes and deleting them (or
    checking for a claim and writing them) is one step no other holder can split.
    """
    computed = {_key_lock_id(key, storage) for key in keys if key}
    if None in computed:
        # A lock that cannot be named is never taken: refused, nothing is touched.
        yield False
        return
    lock_ids = sorted(lock_id for lock_id in computed if lock_id is not None)
    taken: list[int] = []
    try:
        with connection.cursor() as cursor:
            for lock_id in lock_ids:
                if wait:
                    cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
                else:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
                    row = cursor.fetchone()
                    if not (row and row[0]):
                        break
                taken.append(lock_id)
        yield len(taken) == len(lock_ids)
    finally:
        with connection.cursor() as cursor:
            for lock_id in reversed(taken):
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


def ordered_anchors(anchors: Iterable[RetentionAnchor]) -> tuple[RetentionAnchor, ...]:
    """Distinct anchors in a stable order: date, then basis, then source."""
    return tuple(
        sorted(
            set(anchors),
            key=lambda anchor: (anchor.on, anchor.basis, str(anchor.source_id)),
        )
    )


def fiscal_expiry(anchors: Iterable[RetentionAnchor]) -> datetime | None:
    """January 1 of the latest anchored year + 6, in Sao Paulo; None without anchors.

    The latest reference wins, so bytes several records share are kept for the
    longest of their periods. The R1 period stays provisional until OG-08.
    """
    years = [anchor.on.year for anchor in anchors]
    return datetime(max(years) + 6, 1, 1, tzinfo=FISCAL_TIMEZONE) if years else None


def after_years(value: datetime, years: int) -> datetime:
    """Calendar years, including a conservative February leap-day boundary."""
    year = value.year + years
    return value.replace(
        year=year, day=min(value.day, calendar.monthrange(year, value.month)[1])
    )


def retention_decision(
    client_id: UUID, expires_at: datetime | None, *, now: datetime
) -> RetentionDecision:
    """Apply the active hold before interpreting a content expiration."""
    if LegalHold.objects.filter(client_id=client_id, released_at=None).exists():
        return RetentionDecision(eligible=False, reason="legal_hold")
    if expires_at is None:
        return RetentionDecision(eligible=False, reason="unknown_period")
    return RetentionDecision(
        expires_at <= now, "expired" if expires_at <= now else "retained"
    )


@transaction.atomic
def place_hold(
    actor: User, client_id: UUID, *, reason: str, expected_version: int
) -> LegalHold:
    """Client-wide protection; refuse if deletion already crossed its claim point."""
    require_enabled()
    client = load_client(actor, tenant_id(), client_id, lock=True)
    check_version(client.version, expected_version)
    require_actor(actor, "evidence.export")
    if not reason.strip() or len(reason) > MAX_HOLD_REASON:
        msg = "Informe um motivo de até 2.000 caracteres."
        raise ValidationError(msg)
    if ContentPurgeClaim.objects.filter(client=client, completed_at=None).exists():
        msg = "Há uma remoção de conteúdo em andamento. Resolva-a antes de reter."
        raise IllegalTransition(msg)
    hold = LegalHold.objects.create(
        tenant_id=tenant_id(), client=client, reason=reason.strip(), placed_by=actor
    )
    client.version += 1
    client.save(update_fields=["version"])
    record_event(
        action="retention.hold_placed",
        actor=actor,
        obj=ObjectRef("LegalHold", str(hold.pk)),
        metadata={"client_id": str(client.pk)},
    )
    return hold


@transaction.atomic
def release_hold(actor: User, hold_id: UUID, *, expected_version: int) -> LegalHold:
    """Release only the revision the staff member actually reviewed."""
    from django.utils import timezone  # noqa: PLC0415

    require_enabled()
    candidate = LegalHold.objects.filter(pk=hold_id).first()
    if candidate is None:
        raise Http404
    load_client(actor, tenant_id(), candidate.client_id, lock=True)
    hold = LegalHold.objects.select_for_update().get(pk=hold_id)
    check_version(hold.version, expected_version)
    require_actor(actor, "evidence.export")
    if hold.released_at is not None:
        msg = "Esta retenção já foi liberada."
        raise IllegalTransition(msg)
    hold.released_at = timezone.now()
    hold.released_by = actor
    hold.version += 1
    hold.save(update_fields=["released_at", "released_by", "version"])
    record_event(
        action="retention.hold_released",
        actor=actor,
        obj=ObjectRef("LegalHold", str(hold.pk)),
        metadata={"version": hold.version},
    )
    return hold

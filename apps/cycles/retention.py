"""One hold/eligibility decision and explicit, versioned hold commands."""

import calendar
import hashlib
import os
import posixpath
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage, Storage, default_storage
from django.db import connection, transaction
from django.db.models import F, Func, Q, QuerySet, Value
from django.http import Http404

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

    A key string cannot capture the physical file: a symlink anywhere under the
    root, or a hard link, gives one file names no lookup or lock sees. So the root
    must hold no symlink, each key must resolve, inside it, to itself, and each
    existing file must have one link (AXRM-051); then two keys name one object
    exactly when their `physical_key` is the same. Empty when proved, and on any
    backend that is not a local filesystem.
    """
    chosen = default_storage if storage is None else storage
    if not isinstance(chosen, FileSystemStorage):
        return ""
    root = Path(chosen.location).resolve()
    for parent, directories, files in os.walk(root):
        if any((Path(parent) / name).is_symlink() for name in (*directories, *files)):
            return "armazenamento_com_atalho"
    for key in keys:
        path = Path(os.path.normpath(root / key))
        if not path.is_relative_to(root) or path.resolve() != path:
            return "chave_fora_do_armazenamento"
        try:
            links = path.lstat().st_nlink
        except FileNotFoundError:
            continue
        if links > 1:
            return "arquivo_com_vinculos"
    return ""


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


def physical_key(key: str) -> str:
    """Return the one name storage resolves a key to: `a//b` and `a/./b` are `a/b`."""
    return posixpath.normpath(key.replace("\\", "/")).lstrip("/") if key else ""


# Names that resolve to one object differ only in separators and `.` segments, so
# rows are matched with both removed: a coarser match only keeps more.
_LOOSE: Final = r"[/\\.]"


def _loose(key: str) -> str:
    return re.sub(_LOOSE, "", key)


def _naming(
    model: type[Document] | type[DocumentIntake] | type[EvidenceExport],
    field: str,
    keys: Iterable[str],
) -> QuerySet[Any]:
    named = [key for key in keys if key]
    loose = {_loose(key) for key in named} | {_loose(physical_key(k)) for k in named}
    # A `..` segment resolves elsewhere, which stripping cannot see: such rows
    # (restored descriptors, rare) are resolved here, one by one.
    physical = {physical_key(key) for key in named}
    dotted = [
        pk
        for pk, raw in model.objects.filter(**{f"{field}__contains": ".."}).values_list(
            "pk", field
        )
        if physical_key(raw) in physical
    ]
    return model.objects.annotate(
        loose_key=Func(
            F(field), Value(_LOOSE), Value(""), Value("g"), function="regexp_replace"
        )
    ).filter(Q(loose_key__in=loose) | Q(pk__in=dotted))


def storage_key_aliased(
    keys: Iterable[str],
    *,
    document_id: UUID | None = None,
    export_id: UUID | None = None,
) -> bool:
    """Tell whether any row other than this one names one of these storage keys.

    Every row kind that names a key counts: an original's final key, an intake's
    quarantine copy and an evidence archive, of any client of the tenant, under
    any spelling that resolves to the same object.
    """
    named = [key for key in keys if key]
    if not named:
        return False
    finals = _naming(Document, "storage_key", named)
    copies = _naming(DocumentIntake, "quarantine_key", named)
    archives = _naming(EvidenceExport, "storage_key", named)
    if document_id is not None:
        finals = finals.exclude(pk=document_id)
        copies = copies.exclude(document_id=document_id)
    if export_id is not None:
        archives = archives.exclude(pk=export_id)
    return finals.exists() or copies.exists() or archives.exists()


def foreign_storage_keys(tenant_id: UUID, keys: Iterable[str]) -> set[str]:
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
                found.update(_naming(model, field, named).values_list(field, flat=True))
    return {
        key
        for key in named
        for raw in found
        if _loose(key) == _loose(raw) or physical_key(key) == physical_key(raw)
    }


def _key_lock_id(key: str) -> int:
    # One lock per object, whatever spelling of its key a caller holds.
    name = physical_key(key)
    digest = hashlib.sha256(f"axorim-storage-key:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def storage_keys_locked(keys: Iterable[str], *, wait: bool) -> Iterator[bool]:
    """Hold a session advisory lock per storage key; yield whether all were taken.

    Every writer of an original's final key and every disposal of it takes these
    locks, outside any transaction, so verifying the bytes and deleting them (or
    checking for a claim and writing them) is one step no other holder can split.
    """
    lock_ids = sorted({_key_lock_id(key) for key in keys if key})
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

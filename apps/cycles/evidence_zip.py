"""Bounded ZIP_STORED bundles; only exact source bytes qualify as originals."""

import hashlib
import io
import json
import tempfile
import zipfile
from collections.abc import Buffer, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, Final, TypedDict
from uuid import UUID

from django.core.files.storage import Storage
from django.template.loader import render_to_string

from apps.core.tasks import assert_no_transaction
from apps.cycles.archive_storage import open_archive_source
from apps.cycles.effects.manifest import JSONValue

BYTE_CAP: Final = 500 * 1024 * 1024
SPOOL_LIMIT: Final = 16 * 1024 * 1024
METADATA_CAP: Final = 8 * 1024 * 1024
CHUNK: Final = 65536
EXTENSIONS: Final = {
    "application/pdf": "pdf",
    "application/xml": "xml",
    "text/xml": "xml",
    "image/png": "png",
    "image/jpeg": "jpg",
}


class ZipErrorCode(StrEnum):
    """Explain an archive refusal without exposing backend exception text."""

    BYTE_CAP = "byte_cap"
    METADATA_LIMIT = "metadata_limit"
    ORIGINAL_UNAVAILABLE = "original_unavailable"
    ORIGINAL_INELIGIBLE = "original_ineligible"
    SOURCE_CHANGED = "source_changed"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    ALTERNATE_STORAGE_NAME = "alternate_storage_name"


class ZipError(ValueError):
    """A bounded machine code, never storage credentials or source text."""

    def __init__(self, code: ZipErrorCode) -> None:
        """Keep the machine-readable refusal for the task's terminal record."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Original:
    """Authorized materialized descriptor; filenames never determine member paths."""

    id: UUID
    key: str
    sha256: str
    size: int
    content_type: str
    filename: str

    @property
    def member(self) -> str:
        """Derive a safe unique member from trusted identity and media type."""
        return f"originals/{self.id}.{EXTENSIONS.get(self.content_type, 'bin')}"


class ExcludedOriginal(TypedDict):
    """Freeze the identity and eligibility reason without any storage key."""

    id: str
    filename: str
    code: str
    reason: str


class _CappedFile(io.BufferedIOBase):
    """Account for the file extent, including ZIP seeks and its central directory."""

    def __init__(self, file: IO[bytes], limit: int) -> None:
        self.file = file
        self.limit = limit

    def write(self, data: Buffer) -> int:
        size = memoryview(data).nbytes
        if self.file.tell() + size > self.limit:
            raise ZipError(ZipErrorCode.BYTE_CAP)
        return self.file.write(data)

    def tell(self) -> int:
        return self.file.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self.file.seek(offset, whence)

    def flush(self) -> None:
        if not self.file.closed:
            self.file.flush()

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Bundle:
    """Carry the exact closed archive while its owning context stays open."""

    file: IO[bytes]
    byte_size: int
    sha256: str

    @property
    def size(self) -> int:
        """Return the final archive extent, including its central directory."""
        return self.byte_size


def _write_original(
    archive: zipfile.ZipFile, source: Original, storage: Storage, remaining: int
) -> int:
    """Copy bounded chunks and prove actual size/hash before accepting an original."""
    count = 0
    digest = hashlib.sha256()
    try:
        with (
            open_archive_source(storage, source.key) as original,
            archive.open(source.member, "w") as member,
        ):
            while block := original.read(min(CHUNK, remaining - count + 1)):
                count += len(block)
                if count > remaining:
                    raise ZipError(ZipErrorCode.BYTE_CAP)
                digest.update(block)
                member.write(block)
    except OSError as error:
        raise ZipError(ZipErrorCode.ORIGINAL_UNAVAILABLE) from error
    if not count or count != source.size:
        raise ZipError(ZipErrorCode.SIZE_MISMATCH)
    if digest.hexdigest() != source.sha256:
        raise ZipError(ZipErrorCode.HASH_MISMATCH)
    return count


def _metadata_members(
    index_bytes: bytes,
    originals: tuple[Original, ...],
    excluded_originals: tuple[ExcludedOriginal, ...],
    byte_cap: int,
) -> tuple[tuple[str, bytes], ...]:
    """Keep notices bounded and preserve the old metadata when nothing is omitted."""
    excluded_bytes = (
        json.dumps(
            excluded_originals,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if excluded_originals
        else b""
    )
    if len(index_bytes) + len(excluded_bytes) > METADATA_CAP:
        raise ZipError(ZipErrorCode.METADATA_LIMIT)
    html = render_to_string(
        "cycles/evidence_bundle.html",
        {
            "index_text": index_bytes.decode(),
            "originals": originals,
            "excluded_originals": excluded_originals,
        },
    ).encode()
    if len(html) + len(index_bytes) + len(excluded_bytes) > byte_cap:
        raise ZipError(ZipErrorCode.BYTE_CAP)
    members: tuple[tuple[str, bytes], ...] = (
        ("index.json", index_bytes),
        ("index.html", html),
    )
    if excluded_bytes:
        members += (("excluded-originals.json", excluded_bytes),)
    return members


@contextmanager
def build_archive(
    index: dict[str, JSONValue],
    originals: tuple[Original, ...],
    *,
    storage: Storage,
    byte_cap: int = BYTE_CAP,
    excluded_originals: tuple[ExcludedOriginal, ...] = (),
) -> Iterator[Bundle]:
    """Yield a closed, hashed ZIP spool; close every handle even on refusal."""
    unique = tuple({source.id: source for source in originals}.values())
    if sum(source.size for source in unique) > byte_cap:
        raise ZipError(ZipErrorCode.BYTE_CAP)
    with (
        tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as spool,
        tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as metadata,
    ):
        for part in json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).iterencode(index):
            encoded = part.encode()
            if metadata.tell() + len(encoded) > byte_cap:
                raise ZipError(ZipErrorCode.BYTE_CAP)
            if metadata.tell() + len(encoded) > METADATA_CAP:
                raise ZipError(ZipErrorCode.METADATA_LIMIT)
            metadata.write(encoded)
        metadata.seek(0)
        index_bytes = metadata.read(METADATA_CAP + 1)
        members = _metadata_members(index_bytes, unique, excluded_originals, byte_cap)
        manifest = []
        with zipfile.ZipFile(
            _CappedFile(spool, byte_cap), "w", compression=zipfile.ZIP_STORED
        ) as archive:
            for name, content in members:
                archive.writestr(name, content)
                manifest.append(f"{hashlib.sha256(content).hexdigest()}  {name}\n")
            total = 0
            for source in unique:
                total += _write_original(archive, source, storage, byte_cap - total)
                manifest.append(f"{source.sha256}  {source.member}\n")
            archive.writestr("manifest.sha256", "".join(manifest).encode())
        size = spool.seek(0, io.SEEK_END)
        if size > byte_cap:
            raise ZipError(ZipErrorCode.BYTE_CAP)
        spool.seek(0)
        digest = hashlib.sha256()
        while block := spool.read(CHUNK):
            digest.update(block)
        spool.seek(0)
        yield Bundle(spool, size, digest.hexdigest())


@contextmanager
def build_metadata_archive(
    members: Iterable[tuple[str, Iterable[bytes]]], *, byte_cap: int = BYTE_CAP
) -> Iterator[Bundle]:
    """Bound streamed metadata with the same spool, extent cap and manifest as T18."""
    assert_no_transaction()
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as spool:
        manifest = []
        with zipfile.ZipFile(
            _CappedFile(spool, byte_cap), "w", compression=zipfile.ZIP_STORED
        ) as archive:
            for name, chunks in members:
                digest = hashlib.sha256()
                with archive.open(name, "w") as member:
                    for chunk in chunks:
                        assert_no_transaction()
                        member.write(chunk)
                        digest.update(chunk)
                manifest.append(f"{digest.hexdigest()}  {name}\n")
            archive.writestr("manifest.sha256", "".join(manifest).encode())
        size = spool.seek(0, io.SEEK_END)
        spool.seek(0)
        digest = hashlib.sha256()
        while block := spool.read(CHUNK):
            digest.update(block)
        spool.seek(0)
        yield Bundle(spool, size, digest.hexdigest())

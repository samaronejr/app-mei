"""One byte-only export response, safe after the tenant transaction has ended."""

import hashlib
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass

from django.core.files.storage import default_storage
from django.http import StreamingHttpResponse

from apps.core.tasks import assert_no_transaction
from apps.cycles.archive_storage import open_archive_source
from apps.cycles.evidence_zip import (
    BYTE_CAP,
    CHUNK,
    SPOOL_LIMIT,
    ZipError,
    ZipErrorCode,
)


@dataclass(frozen=True, slots=True)
class ArchiveDescriptor:
    """Authorization resolves this descriptor; no model can reach the iterator."""

    key: str
    filename: str
    size: int
    sha256: str


def _chunks(descriptor: ArchiveDescriptor) -> Iterator[bytes]:
    """Validate the bounded copy before emitting it; close on disconnect as well."""
    assert_no_transaction()
    with (
        tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as spool,
        open_archive_source(default_storage, descriptor.key) as original,
    ):
        size = 0
        digest = hashlib.sha256()
        while block := original.read(CHUNK):
            size += len(block)
            if size > min(descriptor.size, BYTE_CAP):
                raise ZipError(ZipErrorCode.BYTE_CAP)
            spool.write(block)
            digest.update(block)
        if size != descriptor.size or digest.hexdigest() != descriptor.sha256:
            raise ZipError(ZipErrorCode.HASH_MISMATCH)
        spool.seek(0)
        while block := spool.read(CHUNK):
            yield block


class EvidenceArchiveResponse(StreamingHttpResponse):
    """TenantMiddleware admits this exact type only on its authorized route."""

    def __init__(self, descriptor: ArchiveDescriptor) -> None:
        """Attach safe headers without opening storage or retaining ORM objects."""
        super().__init__(_chunks(descriptor), content_type="application/zip")
        self["Content-Length"] = str(descriptor.size)
        self["Content-Disposition"] = f'attachment; filename="{descriptor.filename}"'
        self["Cache-Control"] = "private, no-store"
        self["X-Content-Type-Options"] = "nosniff"
        self["Content-Security-Policy"] = "default-src 'none'; sandbox"

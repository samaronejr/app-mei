"""Read export bytes without an eager S3 download ahead of the byte counter."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING, Protocol

from django.core.files import File
from django.core.files.storage import Storage
from storages.backends.s3 import S3Storage
from storages.utils import clean_name, safe_join

from apps.core.tasks import assert_no_transaction

if TYPE_CHECKING:
    from apps.cycles.evidence_zip import Bundle


def upload_archive(storage: Storage, key: str, bundle: "Bundle") -> str:
    """Upload a completed bounded spool, never from a tenant transaction."""
    assert_no_transaction()
    return storage.save(key, File(bundle.file))


class ArchiveSource(Protocol):
    """Only bounded reads and deterministic closure cross the storage boundary."""

    def read(self, size: int, /) -> bytes:
        """Read at most size bytes."""
        ...

    def close(self) -> None:
        """Release the underlying response or local file."""
        ...


@contextmanager
def open_archive_source(storage: Storage, key: str) -> Iterator[ArchiveSource]:
    """Stream S3 GetObject bodies; retain the real filesystem adapter in development."""
    assert_no_transaction()
    if isinstance(storage, S3Storage):
        name = safe_join(storage.location, clean_name(key))
        source: ArchiveSource = storage.bucket.Object(name).get()["Body"]
        with closing(source):
            yield source
    else:
        with storage.open(key, "rb") as source_file:
            stream = source_file.file
            if stream is None:
                message = "Storage returned no readable object."
                raise OSError(message)
            yield stream

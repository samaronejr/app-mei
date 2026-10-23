"""Independent append-only provider receipts; no storage operation holds a DB lock."""

import json
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.core.tasks import assert_no_transaction
from apps.integrations.journal_schema import JournalError, JournalRecord, decode

if TYPE_CHECKING:
    from apps.cycles.effects.manifest import JSONValue

__all__ = [
    "JournalError",
    "JournalRecord",
    "append",
    "epoch_floor",
    "read",
    "record_epoch",
    "tenant_ids",
]

MAX_RECEIPT_BYTES = 16_384


def _paths(prefix: str) -> Iterator[str]:
    try:
        directories, files = default_storage.listdir(prefix)
    except FileNotFoundError:
        return
    for filename in sorted(files):
        yield str(PurePosixPath(prefix, filename))
    for directory in sorted(directories):
        yield from _paths(str(PurePosixPath(prefix, directory)))


def _bytes(path: str) -> bytes:
    with default_storage.open(path, "rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise JournalError
    return bytes(raw)


def _save(path: str, raw: bytes) -> None:
    if default_storage.exists(path):
        if _bytes(path) != raw:
            raise JournalError
        return
    stored = default_storage.save(path, ContentFile(raw))
    if stored != path:
        raise JournalError


def append(record: JournalRecord) -> str:
    """Persist one validated immutable start or result before proceeding."""
    assert_no_transaction()
    raw = record.encode()
    try:
        decode(raw, record.tenant_id)
        _save(record.path, raw)
    except (OSError, BotoCoreError, ClientError, ValueError, TypeError, KeyError):
        raise JournalError from None
    return record.path


def tenant_ids() -> set[UUID]:
    """Expose orphan tenant scopes rather than claiming incomplete replay succeeded."""
    assert_no_transaction()
    try:
        directories, files = default_storage.listdir("provider-journal")
        if files:
            raise JournalError
        return {UUID(name) for name in directories}
    except FileNotFoundError:
        return set()
    except (OSError, BotoCoreError, ClientError, ValueError):
        raise JournalError from None


def read(tenant_id: UUID) -> list[JournalRecord]:
    """Read outside transactions; malformed objects fail the whole inventory."""
    assert_no_transaction()
    try:
        records = []
        for path in _paths(f"provider-journal/{tenant_id}"):
            record = decode(_bytes(path), tenant_id)
            if record.path != path:
                raise JournalError
            records.append(record)
    except (OSError, BotoCoreError, ClientError, ValueError, TypeError, KeyError):
        raise JournalError from None
    return records


def epoch_floor() -> int:
    """Remember restores even when the newer epoch never sent any requests."""
    assert_no_transaction()
    floor = 1
    try:
        for path in _paths("provider-epochs"):
            data: JSONValue = json.loads(_bytes(path))
            if not isinstance(data, dict) or set(data) != {"epoch", "restore_ref"}:
                raise JournalError
            epoch, restore = data["epoch"], data["restore_ref"]
            if type(epoch) is not int or epoch < 1 or not isinstance(restore, str):
                raise JournalError
            if path != f"provider-epochs/{epoch}-{UUID(restore)}.json":
                raise JournalError
            floor = max(floor, epoch)
    except (OSError, BotoCoreError, ClientError, ValueError, TypeError, KeyError):
        raise JournalError from None
    return floor


def record_epoch(epoch: int, restore_ref: UUID) -> None:
    """Write the independent epoch before moving the restored database forward."""
    assert_no_transaction()
    try:
        _save(
            f"provider-epochs/{epoch}-{restore_ref}.json",
            json.dumps(
                {"epoch": epoch, "restore_ref": str(restore_ref)}, sort_keys=True
            ).encode(),
        )
    except (OSError, BotoCoreError, ClientError):
        raise JournalError from None

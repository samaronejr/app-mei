"""Independent append-only objects, never rolled back with the application DB.

Start and reply use distinct immutable names. Bucket versioning/retention and the
operator's restore discipline remain OG-04: FileSystemStorage proves the protocol,
not an object-store's operational retention configuration.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.core.tasks import assert_no_transaction
from apps.delivery.models import MessageState, OutboundMessage

PREFIX = "effects-journal"
DIGEST_LENGTH = 64
OUTCOMES = {
    "intent_started",
    MessageState.ACCEPTED,
    MessageState.REJECTED,
    MessageState.UNKNOWN,
    MessageState.TEMPORARY,
}


class JournalError(Exception):
    """Storage failure: do not send or advertise a successful restore replay."""


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """Schema v1: identifiers, digest, outcome codes and timestamps only."""

    tenant_id: UUID
    client_id: UUID
    intent: str
    digest: str
    attempt_id: UUID
    outcome: str
    occurred_at: str


def append(message: OutboundMessage, outcome: str, *, code: str = "") -> str:
    """Persist a new immutable object before/after the external effect."""
    assert_no_transaction()
    now = timezone.now()
    path = (
        f"{PREFIX}/{message.tenant_id}/{now:%Y/%m}/"
        f"{message.business_intent_key}-{message.lease_owner}-{uuid4()}.json"
    )
    data = {
        "schema_version": 1,
        "tenant_id": str(message.tenant_id),
        "client_id": str(message.client_id),
        "message_id": str(message.pk),
        "intent": message.business_intent_key,
        "digest": message.content_digest,
        "attempt_id": str(message.lease_owner),
        "fence": message.fence,
        "outcome": outcome,
        "code": code,
        "occurred_at": now.isoformat(),
    }
    try:
        return default_storage.save(path, ContentFile(json.dumps(data).encode()))
    except (OSError, BotoCoreError, ClientError):
        msg = "Effect journal write failed."
        raise JournalError(msg) from None


def tenant_ids() -> set[UUID]:
    """Expose orphan journal scopes rather than claiming incomplete replay succeeded."""
    assert_no_transaction()
    try:
        directories, _ = default_storage.listdir(PREFIX)
        return {UUID(name) for name in directories}
    except FileNotFoundError:
        return set()
    except (OSError, BotoCoreError, ClientError, ValueError):
        msg = "Effect journal inventory failed; delivery must stay paused."
        raise JournalError(msg) from None


def _paths(prefix: str) -> Iterator[str]:
    try:
        directories, files = default_storage.listdir(prefix)
    except FileNotFoundError:
        return
    for filename in sorted(files):
        if filename.endswith(".json"):
            yield str(PurePosixPath(prefix, filename))
    for directory in sorted(directories):
        yield from _paths(str(PurePosixPath(prefix, directory)))


def _decode(data: object, tid: UUID) -> JournalRecord:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        msg = "Invalid effect journal schema."
        raise JournalError(msg)
    fields = (
        "tenant_id",
        "client_id",
        "intent",
        "digest",
        "attempt_id",
        "outcome",
        "occurred_at",
    )
    if any(not isinstance(data.get(key), str) for key in fields):
        msg = "Invalid effect journal fields."
        raise JournalError(msg)
    record = JournalRecord(
        tenant_id=UUID(data["tenant_id"]),
        client_id=UUID(data["client_id"]),
        intent=data["intent"],
        digest=data["digest"],
        attempt_id=UUID(data["attempt_id"]),
        outcome=data["outcome"],
        occurred_at=data["occurred_at"],
    )
    if (
        record.tenant_id != tid
        or record.outcome not in OUTCOMES
        or len(record.intent) != DIGEST_LENGTH
        or len(record.digest) != DIGEST_LENGTH
        or datetime.fromisoformat(record.occurred_at).utcoffset() is None
    ):
        msg = "Invalid effect journal record."
        raise JournalError(msg)
    int(record.intent, 16)
    int(record.digest, 16)
    return record


def read(tid: UUID) -> list[JournalRecord]:
    """Read outside transactions; reject malformed or cross-prefix records."""
    assert_no_transaction()
    records = []
    try:
        for path in _paths(f"{PREFIX}/{tid}"):
            with default_storage.open(path, "rb") as stream:
                data = json.load(stream)
            records.append(_decode(data, tid))
    except (OSError, BotoCoreError, ClientError, ValueError, TypeError, KeyError):
        msg = "Effect journal read failed; delivery must stay paused."
        raise JournalError(msg) from None
    return records

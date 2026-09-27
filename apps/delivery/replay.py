"""Restore replay applies independent receipts while all dispatch is paused."""

from collections import defaultdict
from uuid import UUID

from django.conf import settings
from django.core.management.base import CommandError

from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.delivery import journal
from apps.delivery.errors import IntentConflict
from apps.delivery.models import DeliveryEffect, MessageState, OutboundMessage
from apps.delivery.services import effect, record, report_problem

BARRIERS = {MessageState.ACCEPTED, MessageState.UNKNOWN, MessageState.REJECTED}


def _outcome(records: list[journal.JournalRecord]) -> str:
    if any(row.outcome == MessageState.ACCEPTED for row in records):
        return MessageState.ACCEPTED
    started = {row.attempt_id for row in records if row.outcome == "intent_started"}
    completed = {row.attempt_id for row in records if row.outcome != "intent_started"}
    if started - completed:
        return MessageState.UNKNOWN
    return max(records, key=lambda row: row.occurred_at).outcome


def _restore_intent(tid: UUID, records: list[journal.JournalRecord]) -> int:
    digests = {row.digest for row in records}
    if len(digests) != 1:
        msg = "The independent journal contains conflicting digests."
        raise IntentConflict(msg)
    digest = next(iter(digests))
    outcome = _outcome(records)
    client_id, intent = records[0].client_id, records[0].intent
    message = (
        OutboundMessage.objects.select_for_update()
        .filter(client_id=client_id, business_intent_key=intent)
        .first()
    )
    if message is None:
        # Restore can predate the outbox INSERT. Preserve a natural-key barrier.
        if outcome in BARRIERS:
            receipt, created = DeliveryEffect.objects.get_or_create(
                tenant_id=tid,
                client_id=client_id,
                business_intent_key=intent,
                defaults={"content_digest": digest, "outcome": outcome},
            )
            if receipt.content_digest != digest:
                msg = "The restored ledger conflicts with the journal."
                raise IntentConflict(msg)
            return int(created)
        return 0
    if message.content_digest != digest:
        msg = "The restored content conflicts with the journal."
        raise IntentConflict(msg)
    if (
        message.state
        in {
            MessageState.ACCEPTED,
            MessageState.MANUAL_SENT,
            MessageState.MANUAL_NOT_SENT,
        }
        or message.state == outcome
    ):
        return 0
    message.state = outcome
    message.version += 1
    message.last_error_code = "journal_replayed"
    message.save()
    record(message, outcome, code="journal_replayed")
    if outcome in BARRIERS:
        effect(message)
    if outcome in {MessageState.UNKNOWN, MessageState.REJECTED}:
        report_problem(message)
    return 1


def replay_journal() -> int:
    """Leave pause enabled; partial failures cannot authorize network work."""
    assert_no_transaction()
    if not settings.DELIVERY_PAUSED:
        msg = "DELIVERY_PAUSED must be true during restore replay."
        raise CommandError(msg)
    count = 0
    tenants = PlatformTask.tenant_ids(include_inactive=True)
    if journal.tenant_ids() - set(tenants):
        msg = "Journal scopes are missing from the restored DB; keep delivery paused."
        raise CommandError(msg)
    for tid in tenants:
        groups: dict[tuple[UUID, str], list[journal.JournalRecord]] = defaultdict(list)
        for item in journal.read(tid):
            groups[(item.client_id, item.intent)].append(item)
        with tenant_context(tid):
            for records in groups.values():
                count += _restore_intent(tid, records)
    return count

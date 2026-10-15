import copy
from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.core.tenancy import tenant_context
from apps.delivery import journal
from apps.delivery.gateway import send
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.services import claim_batch
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_accepted_journal_prevents_restored_queue_replay(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    assert send(claim).state == MessageState.ACCEPTED
    assert len(server.received) == 1
    # Restore horizon: DB before claim, independent object storage after reply.
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(
            state=MessageState.QUEUED,
            attempts=0,
            fence=0,
            lease_owner=None,
            lease_until=None,
            send_started_at=None,
        )
    server.received.clear()
    with override_settings(DELIVERY_PAUSED=True):
        call_command("reconcile_effects")
    dispatch_outbox()
    # This is the mutation witness: removing journal reads makes it send ONE.
    assert len(server.received) == 0
    assert delivery.reload(message).state == MessageState.ACCEPTED


def test_started_without_reply_is_unknown(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    claim_batch(delivery.graph.tenant.pk)
    journal.append(delivery.reload(message), "intent_started")
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(state=MessageState.QUEUED)
    with override_settings(DELIVERY_PAUSED=True):
        call_command("reconcile_effects")
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.UNKNOWN
    assert not server.received


def test_intent_before_restored_outbox_insert_is_also_blocked(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    original = delivery.queue()
    missing = copy.copy(original)
    missing.pk = uuid4()
    missing.lease_owner = uuid4()
    missing.business_intent_key = replace(delivery.intent, occurrence=2).key(
        delivery.graph.tenant.pk
    )
    journal.append(missing, MessageState.ACCEPTED)
    with override_settings(DELIVERY_PAUSED=True):
        call_command("reconcile_effects")
    recreated = delivery.queue(occurrence=2)
    assert recreated.state == MessageState.ACCEPTED
    dispatch_outbox()
    assert len(server.received) == 1  # only the unrelated original intent


def test_reconciliation_requires_global_pause(delivery: DeliverySetup) -> None:
    with pytest.raises(CommandError, match="DELIVERY_PAUSED"):
        call_command("reconcile_effects")

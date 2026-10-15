"""Explicit retries remain safe across the database/object-store restore horizon."""

import copy
from collections.abc import Callable
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.delivery import journal
from apps.delivery.automations import cancel_dead_letter, retry_dead_letter
from apps.delivery.gateway import send
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.retries import retry_key
from apps.delivery.services import MAX_ATTEMPTS, claim_batch
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("outcome", [MessageState.ACCEPTED, MessageState.UNKNOWN])
def test_successor_journal_barrier_survives_restore_before_its_insert(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    outcome: str,
) -> None:
    # Given the restored parent, but the independent journal already knows its retry.
    peer = smtp_server("reject")
    parent = delivery.queue()
    dispatch_outbox()
    delivery.reload(parent)
    later = copy.copy(parent)
    later.pk = uuid4()
    later.lease_owner = uuid4()
    later.business_intent_key = retry_key(parent.business_intent_key)
    journal.append(later, outcome)
    with override_settings(DELIVERY_PAUSED=True):
        call_command("reconcile_effects")

    # When the manager repeats the same deterministic retry after restore.
    with tenant_context(delivery.graph.tenant.pk):
        successor = retry_dead_letter(
            delivery.graph.actor, parent.pk, expected_version=parent.version
        )

    # Then acceptance or uncertainty is recovered, never resent.
    assert successor.state == outcome
    peer.mode = "accept"
    assert dispatch_outbox() == 0
    assert len(peer.received) == 1


def test_reconcile_does_not_resurrect_a_cancelled_known_refusal(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given an explicit human disposition after the known refusal.
    peer = smtp_server("reject")
    parent = delivery.queue()
    dispatch_outbox()
    delivery.reload(parent)
    with tenant_context(delivery.graph.tenant.pk):
        cancelled = cancel_dead_letter(
            delivery.graph.actor, parent.pk, expected_version=parent.version
        )

    # When the independent journal is reconciled again.
    with override_settings(DELIVERY_PAUSED=True):
        call_command("reconcile_effects")

    # Then the earlier failure receipt cannot undo the later human cancellation.
    assert delivery.reload(parent).state == MessageState.CANCELLED
    assert parent.version == cancelled.version
    assert dispatch_outbox() == 0
    assert len(peer.received) == 1


def test_restore_of_exhausted_temporary_attempt_exposes_a_retryable_refusal(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given five finalized temporary failures and a sixth journaled reply.
    peer = smtp_server("temporary")
    parent = delivery.queue()
    for _ in range(MAX_ATTEMPTS - 1):
        assert dispatch_outbox() == 1
        with tenant_context(delivery.graph.tenant.pk):
            OutboundMessage.objects.filter(pk=parent.pk).update(
                next_attempt_at=timezone.now()
            )
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    assert send(claim).state == MessageState.TEMPORARY

    # When recovery stops before finalize but retains the independent reply.
    with override_settings(DELIVERY_PAUSED=True):
        call_command("reconcile_effects")

    # Then the exhausted row is not left inert in a temporary state.
    assert delivery.reload(parent).state == MessageState.REJECTED
    with tenant_context(delivery.graph.tenant.pk):
        successor = retry_dead_letter(
            delivery.graph.actor, parent.pk, expected_version=parent.version
        )
    peer.mode = "accept"
    assert dispatch_outbox() == 1
    assert delivery.reload(successor).state == MessageState.ACCEPTED
    assert len(peer.received) == MAX_ATTEMPTS + 1

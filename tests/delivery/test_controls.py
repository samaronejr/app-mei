"""Kill switches, quota serialization, backoff and material-change guards."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.delivery.gateway import send
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.services import claim_batch, remaining_quota, set_pause
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_global_and_tenant_pause(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    with override_settings(DELIVERY_PAUSED=True):
        assert dispatch_outbox() == 0
    with tenant_context(delivery.graph.tenant.pk):
        control = set_pause(delivery.graph.actor, paused=True, expected_version=1)
    assert dispatch_outbox() == 0
    with tenant_context(delivery.graph.tenant.pk):
        set_pause(delivery.graph.actor, paused=False, expected_version=control.version)
    assert dispatch_outbox() == 1
    assert delivery.reload(message).state == MessageState.ACCEPTED
    assert len(server.received) == 1


def test_pause_is_checked_after_claim(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    with tenant_context(delivery.graph.tenant.pk):
        set_pause(delivery.graph.actor, paused=True, expected_version=1)
    assert send(claim).state == MessageState.CANCELLED
    assert delivery.reload(message).last_error_code == "delivery_paused"
    assert not server.received


def test_daily_cap_reserves_across_batches(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    for occurrence in range(3):
        delivery.queue(occurrence=occurrence)
    with override_settings(DELIVERY_DAILY_CAP=2):
        assert dispatch_outbox() == 2
        assert dispatch_outbox() == 0
        with tenant_context(delivery.graph.tenant.pk):
            assert remaining_quota() == 0
    assert len(server.received) == 2


def test_retry_backoff_and_six_attempt_limit(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("temporary")
    message = delivery.queue()
    for index, minutes in enumerate((1, 5, 15, 60, 60, 0), start=1):
        before = timezone.now()
        dispatch_outbox()
        delivery.reload(message)
        assert message.attempts == index
        if minutes:
            assert message.next_attempt_at >= before + timedelta(minutes=minutes)
            assert dispatch_outbox() == 0
            with tenant_context(delivery.graph.tenant.pk):
                OutboundMessage.objects.filter(pk=message.pk).update(
                    next_attempt_at=timezone.now()
                )
    assert message.state == MessageState.REJECTED
    assert dispatch_outbox() == 0
    assert len(server.received) == 6


@pytest.mark.parametrize("change", ["cycle", "content"])
def test_material_change_cancels(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP], change: str
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        if change == "cycle":
            ServiceCycle.objects.filter(pk=delivery.cycle.pk).update(
                state=CycleState.COMPLETED
            )
        else:
            OutboundMessage.objects.filter(pk=message.pk).update(subject="changed")
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.CANCELLED
    assert not server.received


def test_unflagged_tenant_is_not_dispatched(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    delivery.graph.tenant.features = []
    delivery.graph.tenant.save(update_fields=["features"])
    assert dispatch_outbox() == 0
    assert delivery.reload(message).state == MessageState.QUEUED


@pytest.mark.parametrize("condition", ["unknown", "future", "exhausted"])
def test_claim_only_due_retryable_rows(delivery: DeliverySetup, condition: str) -> None:
    message = delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        changes: dict[str, dict[str, object]] = {
            "unknown": {"state": MessageState.UNKNOWN},
            "future": {"next_attempt_at": timezone.now() + timedelta(days=1)},
            "exhausted": {"attempts": 6},
        }
        OutboundMessage.objects.filter(pk=message.pk).update(**changes[condition])
    assert claim_batch(delivery.graph.tenant.pk) == []

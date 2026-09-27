"""Firm decisions repeat portfolio authorization and optimistic concurrency."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from apps.core.tenancy import tenant_context
from apps.delivery.errors import VersionConflict
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.services import decide, set_pause
from apps.tenants.models import Membership
from tests.delivery.conftest import DeliverySetup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def test_unassigned_actor_404s_without_writes(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        before = DeliveryAttempt.objects.count()
        with pytest.raises(Http404):
            decide(
                delivery.graph.staff, message.pk, expected_version=1, action="cancel"
            )
        with pytest.raises(Http404):
            enqueue(
                delivery.graph.staff,
                delivery.graph.client.pk,
                delivery.contact.pk,
                intent=delivery.intent,
                expected_version=1,
            )
        assert DeliveryAttempt.objects.count() == before
        assert OutboundMessage.objects.count() == 1


def test_revoked_actor_cannot_change_an_old_message(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    Membership.objects.filter(user=delivery.graph.actor).update(is_active=False)
    with (
        tenant_context(delivery.graph.tenant.pk),
        pytest.raises((Http404, PermissionDenied)),
    ):
        decide(delivery.graph.actor, message.pk, expected_version=1, action="cancel")
    assert delivery.reload(message).state == MessageState.QUEUED


def test_competing_human_decisions_have_one_winner(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(state=MessageState.UNKNOWN)
        before = DeliveryAttempt.objects.count()
    barrier = Barrier(2, timeout=10)

    def caller(action: str) -> str:
        def decide_now() -> str:
            barrier.wait()
            try:
                decide(
                    delivery.graph.actor,
                    message.pk,
                    expected_version=1,
                    action=action,
                    note=f"Decision {action}",
                )
            except VersionConflict:
                return "conflict"
            return "changed"

        return run_in_thread_as_runtime(delivery.graph.tenant.pk, decide_now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(caller, "sent")
        b = pool.submit(caller, "not_sent")
        assert sorted([a.result(timeout=30), b.result(timeout=30)]) == [
            "changed",
            "conflict",
        ]
    with tenant_context(delivery.graph.tenant.pk):
        assert DeliveryAttempt.objects.count() == before + 1
    assert delivery.reload(message).state in {
        MessageState.MANUAL_SENT,
        MessageState.MANUAL_NOT_SENT,
    }
    assert message.version == 2
    assert message.decision_note


def test_unknown_needs_note_and_never_becomes_smtp_evidence(
    delivery: DeliverySetup,
) -> None:
    message = delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(state=MessageState.UNKNOWN)
        with pytest.raises(ValidationError):
            decide(delivery.graph.actor, message.pk, expected_version=1, action="sent")
        result = decide(
            delivery.graph.actor,
            message.pk,
            expected_version=1,
            action="sent",
            note="Manual mailbox check",
        )
        assert result.state == MessageState.MANUAL_SENT
        with pytest.raises(VersionConflict):
            decide(
                delivery.graph.actor,
                message.pk,
                expected_version=1,
                action="not_sent",
                note="stale",
            )


def test_stale_pause_has_no_event_or_state_write(delivery: DeliverySetup) -> None:
    with tenant_context(delivery.graph.tenant.pk):
        set_pause(delivery.graph.actor, paused=True, expected_version=1)

    def stale() -> None:
        with pytest.raises(VersionConflict):
            set_pause(delivery.graph.actor, paused=False, expected_version=1)

    run_in_thread_as_runtime(delivery.graph.tenant.pk, stale)

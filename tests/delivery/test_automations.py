"""Automation commands preserve tenant scope, revisions and immutable receipts."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404

from apps.audit.models import Event
from apps.authz.models import Capability, GrantLevel, UserCapabilityGrant
from apps.core.tenancy import tenant_context
from apps.delivery.automations import cancel_dead_letter, retry_dead_letter
from apps.delivery.errors import VersionConflict
from apps.delivery.models import (
    AutomationPause,
    DeliveryAttempt,
    DeliveryEffect,
    MessageState,
    OutboundMessage,
)
from apps.delivery.tasks import dispatch_outbox
from apps.tenants.models import Membership
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.delivery.test_boundaries import _web
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)

URL = "/gestao/automacoes/"


def _rejected(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> tuple[OutboundMessage, FakeSMTP]:
    server = smtp_server("reject")
    message = delivery.queue()
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.REJECTED
    assert len(server.received) == 1
    return message, server


def _grant_manager(delivery: DeliverySetup) -> None:
    with tenant_context(delivery.graph.tenant.pk):
        UserCapabilityGrant.objects.create(
            tenant=delivery.graph.tenant,
            user=delivery.graph.staff,
            capability=Capability.objects.get(slug="automations.manage"),
            level=GrantLevel.FULL,
            granted_by=delivery.graph.actor,
        )


def test_retry_preserves_refusal_and_sends_one_successor(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given a real, definitive SMTP refusal with an immutable effect barrier.
    message, server = _rejected(delivery, smtp_server)
    with tenant_context(delivery.graph.tenant.pk):
        old_receipt = DeliveryEffect.objects.get(message=message)
        old_attempts = list(
            DeliveryAttempt.objects.filter(message=message).values_list("pk", flat=True)
        )

        # When a manager explicitly retries the rendered version.
        successor = retry_dead_letter(
            delivery.graph.actor, message.pk, expected_version=message.version
        )

        # Then history survives, and only one new envelope is authorized.
        assert successor.retry_of_id == message.pk
        assert successor.pk != message.pk
        assert successor.content_digest == message.content_digest
        assert successor.recipients_json == message.recipients_json
        assert DeliveryEffect.objects.get(pk=old_receipt.pk).outcome == (
            MessageState.REJECTED
        )
        assert set(old_attempts) <= set(
            DeliveryAttempt.objects.filter(message=message).values_list("pk", flat=True)
        )
        assert Event.objects.filter(action="delivery.dead_letter_retried").count() == 1
    server.mode = "accept"
    assert dispatch_outbox() == 1
    assert dispatch_outbox() == 0
    assert len(server.received) == 2
    assert delivery.reload(successor).state == MessageState.ACCEPTED
    assert delivery.reload(message).state == MessageState.CANCELLED
    with tenant_context(delivery.graph.tenant.pk):
        assert DeliveryEffect.objects.filter(message=successor).get().outcome == (
            MessageState.ACCEPTED
        )


def test_cancel_preserves_refusal_without_resending(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given a known refused envelope.
    message, server = _rejected(delivery, smtp_server)
    with tenant_context(delivery.graph.tenant.pk):
        receipt = DeliveryEffect.objects.get(message=message)

        # When the manager cancels it.
        cancel_dead_letter(
            delivery.graph.actor, message.pk, expected_version=message.version
        )

        # Then its immutable rejection survives the audited disposition.
        assert (
            DeliveryEffect.objects.get(pk=receipt.pk).outcome == MessageState.REJECTED
        )
        assert (
            Event.objects.filter(action="delivery.dead_letter_cancelled").count() == 1
        )
    assert delivery.reload(message).state == MessageState.CANCELLED
    assert dispatch_outbox() == 0
    assert len(server.received) == 1


@pytest.mark.parametrize("mode", ["disconnect", "accept"])
@pytest.mark.parametrize("action", ["retry", "cancel"])
def test_uncertain_and_accepted_messages_refuse_dead_letter_actions(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    mode: str,
    action: str,
) -> None:
    # Given a real ambiguous or accepted effect, not a known refusal.
    server = smtp_server(mode)
    message = delivery.queue()
    dispatch_outbox()
    delivery.reload(message)
    with tenant_context(delivery.graph.tenant.pk):
        attempts = DeliveryAttempt.objects.count()
        events = Event.objects.count()
        command = retry_dead_letter if action == "retry" else cancel_dead_letter

        # When a forged direct command tries to reuse the dead-letter controls.
        with pytest.raises(ValidationError):
            command(delivery.graph.actor, message.pk, expected_version=message.version)

        # Then it writes neither a receipt nor an envelope nor an audit success.
        assert DeliveryAttempt.objects.count() == attempts
        assert Event.objects.count() == events
        assert OutboundMessage.objects.count() == 1
    assert dispatch_outbox() == 0
    assert len(server.received) == 1


def test_unassigned_manager_sees_no_client_and_cannot_retry(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given a manager capability with no assignment to this client.
    message, _server = _rejected(delivery, smtp_server)
    _grant_manager(delivery)
    web = _web(delivery, delivery.graph.staff)

    # When the actor lists the page and forges the hidden message id.
    response = web.get(URL)
    assert response.status_code == 200
    assert str(message.pk).encode() not in response.content
    assert delivery.graph.client.legal_name.encode() not in response.content
    with tenant_context(delivery.graph.tenant.pk):
        attempts = DeliveryAttempt.objects.count()
    response = web.post(
        URL,
        {
            "message_id": str(message.pk),
            "expected_version": message.version,
            "action": "retry",
        },
    )

    # Then the scoped id is refused without a write.
    assert response.status_code == 404
    with tenant_context(delivery.graph.tenant.pk):
        assert DeliveryAttempt.objects.count() == attempts
        assert OutboundMessage.objects.count() == 1
        with pytest.raises(Http404):
            retry_dead_letter(
                delivery.graph.staff, message.pk, expected_version=message.version
            )


def test_page_and_mutations_require_automation_management(
    delivery: DeliverySetup,
) -> None:
    # Given an authenticated staff member without automations.manage.
    web = _web(delivery, delivery.graph.staff)

    # When both read and write surfaces are requested.
    assert web.get(URL).status_code == 403
    assert web.post(URL, {"action": "pause", "expected_version": 1}).status_code == 403

    # Then no coordination row was created by the refused request.
    with tenant_context(delivery.graph.tenant.pk):
        assert not AutomationPause.objects.exists()


def test_pause_form_carries_revision_and_stale_post_cannot_resume(
    delivery: DeliverySetup,
) -> None:
    # Given the initial rendered tenant pause revision.
    web = _web(delivery, delivery.graph.actor)
    assert web.get(URL).status_code == 200
    assert web.post(URL, {"action": "pause", "expected_version": 1}).status_code == 302
    with tenant_context(delivery.graph.tenant.pk):
        before = Event.objects.filter(action="delivery.pause_changed").count()

    # When an old form tries to undo that pause.
    response = web.post(URL, {"action": "unpause", "expected_version": 1})

    # Then the conflict neither writes nor clears the pause.
    assert response.status_code == 409
    with tenant_context(delivery.graph.tenant.pk):
        control = AutomationPause.objects.get()
        assert control.paused
        assert control.version == 2
        assert Event.objects.filter(action="delivery.pause_changed").count() == before


def test_competing_retry_and_cancel_have_one_winner(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given two managers acting on the same rendered refusal.
    message, _server = _rejected(delivery, smtp_server)
    barrier = Barrier(2, timeout=10)

    def caller(action: str) -> str:
        def change() -> str:
            barrier.wait()
            command = retry_dead_letter if action == "retry" else cancel_dead_letter
            try:
                command(
                    delivery.graph.actor, message.pk, expected_version=message.version
                )
            except VersionConflict:
                return "conflict"
            return "changed"

        return run_in_thread_as_runtime(delivery.graph.tenant.pk, change)

    # When the two real runtime-role transactions contend.
    with ThreadPoolExecutor(max_workers=2) as pool:
        retry = pool.submit(caller, "retry")
        cancel = pool.submit(caller, "cancel")
        results = [retry.result(timeout=30), cancel.result(timeout=30)]

    # Then one wins and the stale caller cannot append a second decision.
    assert sorted(results) == ["changed", "conflict"]
    with tenant_context(delivery.graph.tenant.pk):
        assert (
            Event.objects.filter(
                action__in=[
                    "delivery.dead_letter_retried",
                    "delivery.dead_letter_cancelled",
                ]
            ).count()
            == 1
        )


def test_revoked_member_cannot_use_rendered_controls(
    delivery: DeliverySetup,
) -> None:
    # Given a valid initial form followed by a membership revocation.
    web = _web(delivery, delivery.graph.actor)
    assert web.get(URL).status_code == 200
    Membership.objects.filter(user=delivery.graph.actor).update(is_active=False)

    # When the old session submits its form.
    response = web.post(URL, {"action": "pause", "expected_version": 1})

    # Then revocation fails closed without a control write.
    assert response.status_code in {403, 404}
    with tenant_context(delivery.graph.tenant.pk):
        assert not AutomationPause.objects.exists()

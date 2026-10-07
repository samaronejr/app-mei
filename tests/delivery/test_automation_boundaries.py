"""Malformed forms and cross-client references cannot write automation state."""

from collections.abc import Callable
from dataclasses import replace

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404

from apps.audit.models import Event
from apps.clients.models import ClientCompany, ClientContact, Engagement
from apps.core.tenancy import tenant_context
from apps.delivery.automations import cancel_dead_letter, retry_dead_letter
from apps.delivery.models import AutomationPause, MessageState, OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.tasks import dispatch_outbox
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.delivery.test_boundaries import _web

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"action": "pause", "expected_version": "zero"},
        {"action": "pause", "expected_version": "-1"},
        {"action": "pause", "expected_version": "0"},
        {"action": "retry", "expected_version": "1"},
        {"action": "retry", "expected_version": "1", "message_id": "not-a-uuid"},
        {"action": "send-now", "expected_version": "1"},
    ],
)
def test_malformed_form_is_422_without_control_or_audit_write(
    delivery: DeliverySetup, data: dict[str, str]
) -> None:
    # Given an authorized actor and malformed boundary input.
    web = _web(delivery, delivery.graph.actor)
    with tenant_context(delivery.graph.tenant.pk):
        before = Event.objects.filter(action__startswith="delivery.").count()

    # When the real view parses the form.
    response = web.post("/gestao/automacoes/", data)

    # Then neither a coordination row nor a success audit is written.
    assert response.status_code == 422
    with tenant_context(delivery.graph.tenant.pk):
        assert not AutomationPause.objects.exists()
        assert Event.objects.filter(action__startswith="delivery.").count() == before


def test_foreign_tenant_message_is_404_without_a_coordination_write(
    delivery: DeliverySetup,
) -> None:
    # Given a real message in one firm and an authorized owner in a different firm.
    message = delivery.queue()
    other = cycle_setup("other-automation")

    # When the second owner supplies the first firm's id.
    with tenant_context(other.tenant.pk):
        with pytest.raises(Http404):
            retry_dead_letter(other.actor, message.pk, expected_version=1)

        # Then even the lazy control row is rolled back with the refusal.
        assert not AutomationPause.objects.exists()
        assert not OutboundMessage.objects.exists()


def test_retry_reference_cannot_cross_clients_in_the_same_tenant(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given a legitimate successor plus another client's legitimate envelope.
    smtp_server("reject")
    original = delivery.queue()
    dispatch_outbox()
    delivery.reload(original)
    with tenant_context(delivery.graph.tenant.pk):
        successor = retry_dead_letter(
            delivery.graph.actor, original.pk, expected_version=original.version
        )
        other = ClientCompany.objects.create(
            tenant=delivery.graph.tenant,
            legal_name="Outro cliente sintético",
            cnpj="12345678000195",
        )
        engagement = Engagement.objects.create(
            tenant=delivery.graph.tenant,
            client=other,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=delivery.graph.actor,
            provenance="staff_entry",
        )
        contact = ClientContact.objects.create(
            tenant=delivery.graph.tenant,
            client=other,
            name="Outro contato sintético",
            email="other@example.test",
        )
        foreign = enqueue(
            delivery.graph.actor,
            other.pk,
            contact.pk,
            intent=replace(delivery.intent, engagement=engagement.pk),
            expected_version=1,
        )

        # When a raw write bypasses the service's same-client check.
        with (
            pytest.raises(IntegrityError, match="outbound_retry_client_fk"),
            transaction.atomic(),
        ):
            OutboundMessage.objects.filter(pk=successor.pk).update(
                retry_of_id=foreign.pk
            )

        # Then the composite FK, not a tenant-only FK, rejects the reference.
        successor.refresh_from_db()
        assert successor.retry_of_id == original.pk


def test_unflagged_automation_surface_is_absent(delivery: DeliverySetup) -> None:
    # Given a firm without the R1 feature.
    delivery.graph.tenant.features = []
    delivery.graph.tenant.save(update_fields=["features"])

    # When its owner follows a previously known URL.
    response = _web(delivery, delivery.graph.actor).get("/gestao/automacoes/")

    # Then no inert automation controls are exposed.
    assert response.status_code == 404


@pytest.mark.parametrize("mode", ["accept", "disconnect"])
def test_mutable_status_cannot_override_an_accepted_or_uncertain_receipt(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP], mode: str
) -> None:
    # Given a real receipt followed by inconsistent mutable envelope state.
    smtp_server(mode)
    message = delivery.queue()
    dispatch_outbox()
    delivery.reload(message)
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(
            state=MessageState.REJECTED
        )
        events = Event.objects.count()

        # When either control is called against that apparent dead letter.
        for command in (retry_dead_letter, cancel_dead_letter):
            with pytest.raises(ValidationError):
                command(
                    delivery.graph.actor,
                    message.pk,
                    expected_version=message.version,
                )

        # Then the immutable receipt prevails, with no successor or audit success.
        assert OutboundMessage.objects.count() == 1
        assert Event.objects.count() == events

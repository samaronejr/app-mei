from collections.abc import Callable

import pytest
from django.core.exceptions import ValidationError

from apps.clients.models import ClientCompany, ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.models import MessageState
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "value", ["someone@example.test", "contact.pdf", "bad\nBcc: attacker@example.test"]
)
def test_free_text_cannot_select_a_recipient(
    delivery: DeliverySetup, value: str
) -> None:
    with pytest.raises(ValidationError):
        delivery.queue(contact_id=value)


@pytest.mark.parametrize("field", ["active", "channel_email_allowed"])
def test_contact_must_be_active_and_consented(
    delivery: DeliverySetup, field: str
) -> None:
    with tenant_context(delivery.graph.tenant.pk):
        ClientContact.objects.filter(pk=delivery.contact.pk).update(**{field: False})
    with pytest.raises(ValidationError):
        delivery.queue()


def test_header_injection_in_stored_address_is_refused(delivery: DeliverySetup) -> None:
    with tenant_context(delivery.graph.tenant.pk):
        ClientContact.objects.filter(pk=delivery.contact.pk).update(
            email="good@example.test\nBcc: attacker@example.test"
        )
    with pytest.raises(ValidationError):
        delivery.queue()


@pytest.mark.parametrize(
    "change",
    [
        {"active": False},
        {"channel_email_allowed": False},
        {"email": "different@example.test"},
    ],
)
def test_contact_is_reread_before_send(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    change: dict[str, object],
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        ClientContact.objects.filter(pk=delivery.contact.pk).update(**change)
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.CANCELLED
    assert not server.received


def test_other_client_contact_is_not_a_recipient(delivery: DeliverySetup) -> None:
    with tenant_context(delivery.graph.tenant.pk):
        other = ClientCompany.objects.create(
            tenant=delivery.graph.tenant,
            legal_name="Other synthetic company",
            cnpj="12345678000195",
            status="active",
        )
        contact = ClientContact.objects.create(
            tenant=delivery.graph.tenant,
            client=other,
            name="Other",
            email="other@example.test",
        )
    with pytest.raises(ValidationError):
        delivery.queue(contact_id=contact.pk)

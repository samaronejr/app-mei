from collections.abc import Callable

import pytest

from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.errors import IntentConflict
from apps.delivery.models import DeliveryEffect
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_same_intent_one_effect(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    first = delivery.queue()
    assert delivery.queue().pk == first.pk
    dispatch_outbox()
    dispatch_outbox()
    assert len(server.received) == 1
    with tenant_context(delivery.graph.tenant.pk):
        assert DeliveryEffect.objects.count() == 1
    assert b"Message-ID:" in server.received[0]
    assert b"Content-Disposition: attachment" not in server.received[0]


def test_same_key_different_digest_conflicts(delivery: DeliverySetup) -> None:
    delivery.queue()
    with tenant_context(delivery.graph.tenant.pk):
        ClientContact.objects.filter(pk=delivery.contact.pk).update(
            email="changed@example.test"
        )
    with pytest.raises(IntentConflict):
        delivery.queue()

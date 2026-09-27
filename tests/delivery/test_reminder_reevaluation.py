"""T10 registry contract; real request/upload integration belongs to T11."""

from collections.abc import Callable

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.policy import KINDS, MessageKind
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_retry_rechecks_registered_domain_state(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_open = True
    monkeypatch.setitem(
        KINDS,
        "reminder",
        MessageKind(
            KINDS["notice"].subject,
            KINDS["notice"].template,
            lambda message: (request_open, "request_closed"),
        ),
    )
    server = smtp_server("temporary")
    message = delivery.queue(kind="reminder")
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.TEMPORARY
    request_open = False
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(
            next_attempt_at=timezone.now()
        )
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.CANCELLED
    assert message.last_error_code == "request_closed"
    assert len(server.received) == 1

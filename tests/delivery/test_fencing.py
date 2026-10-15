from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.delivery.contracts import Reply
from apps.delivery.errors import StaleFenceError
from apps.delivery.gateway import send
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.services import claim_batch, finalize
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_stale_fence_cannot_send_or_finalize(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    stale = replace(claim, fence=claim.fence - 1)
    with pytest.raises(StaleFenceError):
        send(stale)
    assert not finalize(stale, Reply(MessageState.ACCEPTED))
    with tenant_context(delivery.graph.tenant.pk):
        assert DeliveryAttempt.objects.filter(
            message=message,
            fence=stale.fence,
            attempt_id=stale.owner,
            error_code="stale_fence",
        ).exists()
    assert delivery.reload(message).state == MessageState.SENDING
    assert not server.received
    assert finalize(claim, send(claim))
    with pytest.raises(StaleFenceError):
        send(claim)
    assert len(server.received) == 1


def test_expired_sending_lease_is_not_reclaimed(delivery: DeliverySetup) -> None:
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    with tenant_context(delivery.graph.tenant.pk):
        OutboundMessage.objects.filter(pk=message.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    with pytest.raises(StaleFenceError):
        send(claim)
    assert claim_batch(delivery.graph.tenant.pk) == []
    assert delivery.reload(message).state == MessageState.UNKNOWN
    assert message.attempts == 1

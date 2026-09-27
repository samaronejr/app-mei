from collections.abc import Callable

import pytest

from apps.core.tenancy import tenant_context
from apps.delivery.contracts import Reply
from apps.delivery.gateway import send
from apps.delivery.models import DeliveryAttempt, MessageState
from apps.delivery.services import claim_batch, finalize
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


def test_late_statuses_recorded_without_regression(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    smtp_server("accept")
    message = delivery.queue()
    claim = claim_batch(delivery.graph.tenant.pk)[0]
    finalize(claim, send(claim))
    for state in (MessageState.TEMPORARY, MessageState.UNKNOWN, MessageState.REJECTED):
        assert not finalize(claim, Reply(state))
    assert delivery.reload(message).state == MessageState.ACCEPTED
    with tenant_context(delivery.graph.tenant.pk):
        assert (
            DeliveryAttempt.objects.filter(error_code="late_observation").count() == 3
        )

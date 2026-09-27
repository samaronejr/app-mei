from collections.abc import Callable

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.delivery.models import MessageState
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("mode", ["disconnect", "hang"])
def test_drop_after_data_is_unknown_and_never_retried(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP], mode: str
) -> None:
    server = smtp_server(mode)
    message = delivery.queue()
    dispatch_outbox()
    assert delivery.reload(message).state == MessageState.UNKNOWN
    dispatch_outbox()
    assert len(server.received) == 1
    with tenant_context(delivery.graph.tenant.pk):
        assert (
            CycleException.objects.filter(
                subject_ref=message.pk, reason_code="delivery_unknown"
            ).count()
            == 1
        )

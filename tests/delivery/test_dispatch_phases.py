from collections.abc import Callable

import pytest
from django.db import connection

from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_tenant
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def test_claim_is_committed_before_data(
    delivery: DeliverySetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    server = smtp_server("accept")
    message = delivery.queue()
    observed: list[str] = []

    def before_network_io() -> None:
        assert not connection.in_atomic_block
        assert not server.received
        observed.append(
            run_in_thread_as_runtime(
                delivery.graph.tenant.pk,
                lambda: OutboundMessage.objects.get(pk=message.pk).state,
            )
        )

    dispatch_tenant(str(delivery.graph.tenant.pk), before_network_io=before_network_io)
    assert observed == [MessageState.SENDING]
    assert len(server.received) == 1
    assert delivery.reload(message).state == MessageState.ACCEPTED

from collections.abc import Callable

import pytest

from apps.delivery.models import MessageState
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.conftest import DeliverySetup
from tests.delivery.smtp import FakeSMTP

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("reject", MessageState.REJECTED),
        ("rcpt_reject", MessageState.REJECTED),
        ("temporary", MessageState.TEMPORARY),
    ],
)
def test_rcpt_does_not_assert_data_acceptance(
    delivery: DeliverySetup,
    smtp_server: Callable[[str], FakeSMTP],
    mode: str,
    expected: str,
) -> None:
    server = smtp_server(mode)
    message = delivery.queue()
    dispatch_outbox()
    assert server.rcpt_seen.is_set()
    assert delivery.reload(message).state == expected

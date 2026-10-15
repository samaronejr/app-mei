"""Paused or disabled auto occurrences cancel even before a quota-reserving claim."""

from collections.abc import Callable

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.delivery import journal, tasks
from apps.delivery.models import AutomationPause, MessageState
from apps.delivery.services import set_pause
from tests.delivery import reminder_fixtures
from tests.delivery.reminder_fixtures import NOW, allow_automatic, reminders
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
reminder_setup = reminder_fixtures.reminder_setup


@pytest.mark.parametrize("change", ["tenant_pause", "global_pause", "flag"])
def test_pending_automatic_occurrence_cancels_without_claiming_quota(
    reminder_setup: PortalSetup,
    smtp_server: Callable[[str], FakeSMTP],
    settings: SettingsWrapper,
    change: str,
) -> None:
    # Given a queued automatic occurrence whose control changes before any claim.
    setup = reminder_setup
    allow_automatic(setup)
    tasks.sweep_reminders(clock=lambda: NOW)
    if change == "tenant_pause":
        with tenant_context(setup.graph.tenant.pk):
            set_pause(setup.graph.actor, paused=True, expected_version=1)
    elif change == "global_pause":
        settings.DELIVERY_PAUSED = True
    else:
        setup.graph.tenant.features = []
        setup.graph.tenant.save(update_fields=["features"])
    server = smtp_server("accept")

    # When the regular dispatcher reaches its pre-claim obsolete-reminder phase.
    tasks.dispatch_outbox()

    # Then the auto notice is cancelled while manual notices keep their pause contract.
    [message] = reminders(setup)
    assert message.state == MessageState.CANCELLED
    assert message.last_error_code == "delivery_paused"
    assert message.attempts == 0
    assert message.send_started_at is None
    with tenant_context(setup.graph.tenant.pk):
        assert AutomationPause.objects.get().reserved_today == 0
    assert server.received == []
    assert journal.read(setup.graph.tenant.pk) == []

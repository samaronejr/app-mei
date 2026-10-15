"""T39 uses an explicit owner opt-in, a fixed clock and the real request services."""

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.services.requests import RequestFields, update_request
from apps.delivery.models import OutboundMessage, TenantNoticePolicy
from apps.delivery.services import decide
from tests.portal.request_fixtures import PortalSetup, portal_setup

NOW = datetime(2030, 10, 12, 12, tzinfo=UTC)


def overdue_setup(slug: str) -> PortalSetup:
    """Make one overdue request; unrelated manual notices are explicitly cancelled."""
    setup = portal_setup(slug)
    with tenant_context(setup.graph.tenant.pk):
        setup.request = update_request(
            setup.graph.actor,
            setup.request.pk,
            RequestFields(
                title=setup.request.title,
                due_on=timezone.localdate(NOW) - timedelta(days=1),
            ),
            expected_version=setup.request.version,
        )
        for message in OutboundMessage.objects.filter(kind="request_notice"):
            decide(
                setup.graph.actor,
                message.pk,
                expected_version=message.version,
                action="cancel",
            )
    return setup


@pytest.fixture
def reminder_setup(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PortalSetup:
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    return overdue_setup("reminder-sweep")


def allow_automatic(setup: PortalSetup) -> TenantNoticePolicy:
    """Opt in through the real owner form, rather than planting consent in a fixture."""
    response = setup.firm().post(
        reverse("notice-policy"),
        {
            "action": "save",
            "expected_version": 0,
            "automatic_reminders": "on",
            "acknowledged": "on",
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        policy = TenantNoticePolicy.objects.get()
        assert policy.grants_json == [
            {"kind": "request_reminder", "mode": "automatic", "rule_version": 1}
        ]
        return policy


def reminders(setup: PortalSetup) -> list[OutboundMessage]:
    with tenant_context(setup.graph.tenant.pk):
        return list(
            OutboundMessage.objects.filter(kind="request_reminder").order_by("pk")
        )

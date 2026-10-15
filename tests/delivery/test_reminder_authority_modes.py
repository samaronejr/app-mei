"""Automatic authority is explicit consent, not an impersonated manual actor."""

from collections.abc import Callable
from http import HTTPStatus

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.authz.models import GrantLevel, RoleGrant
from apps.core.tenancy import tenant_context
from apps.cycles.services.requests import remind_request
from apps.delivery import tasks
from apps.delivery.models import TenantNoticePolicy
from apps.tenants.models import TenantRole
from tests.delivery import reminder_fixtures
from tests.delivery.reminder_fixtures import NOW, allow_automatic, reminders
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
reminder_setup = reminder_fixtures.reminder_setup


def test_automatic_opt_in_without_acknowledgement_has_no_policy_write(
    reminder_setup: PortalSetup,
) -> None:
    # Given an actual owner and an automatic selection without the acknowledgement.
    setup = reminder_setup

    # When the existing private owner form receives that selection.
    response = setup.firm().post(
        reverse("notice-policy"),
        {"action": "save", "expected_version": 0, "automatic_reminders": "on"},
    )

    # Then the new mode cannot inherit consent from publication, a flag or a checkbox.
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    with tenant_context(setup.graph.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()


def test_system_grant_does_not_impersonate_the_owners_manual_capability(
    reminder_setup: PortalSetup, smtp_server: Callable[[str], FakeSMTP]
) -> None:
    # Given valid automations owner consent but no manual cycles.operate permission.
    setup = reminder_setup
    allow_automatic(setup)
    RoleGrant.objects.filter(
        role=TenantRole.OWNER, capability__slug="cycles.operate"
    ).update(level=GrantLevel.NONE)
    with tenant_context(setup.graph.tenant.pk), pytest.raises(PermissionDenied):
        remind_request(
            setup.graph.actor,
            setup.request.pk,
            expected_version=setup.request.version,
        )
    server = smtp_server("accept")

    # When the actual bounded system rule, then the existing dispatcher, run.
    result = tasks.sweep_reminders(clock=lambda: NOW)
    tasks.dispatch_outbox()

    # Then system consent authorizes only the templated notice, without a fake session.
    assert result["queued"] == 1
    assert len(reminders(setup)) == 1
    assert len(server.received) == 1

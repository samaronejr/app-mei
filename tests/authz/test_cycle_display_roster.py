"""The batched exception roster must equal each member's live portfolio rule."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.models import Capability, GrantLevel, RoleGrant, UserCapabilityGrant
from apps.authz.portfolio import PortfolioScope, portfolio_scope
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.exception_context import _roster
from apps.tenants.models import Membership, TenantRole
from tests.cycles.cycle_fixtures import cycle_setup

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("grant_state", ["role", "raised", "expired", "revoked"])
def test_display_roster_equals_live_portfolio_with_ceiling_controls(
    monkeypatch: pytest.MonkeyPatch, grant_state: str
) -> None:
    # Given assigned staff, unassigned staff and a firm-wide member.
    moment = datetime(2026, 9, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    setup = cycle_setup(f"t36-roster-{grant_state}")
    operations = User.objects.create_user(email=f"ops@{setup.tenant.slug}.test")
    unassigned = User.objects.create_user(email=f"outside@{setup.tenant.slug}.test")
    Membership.objects.create(
        tenant=setup.tenant, user=operations, role=TenantRole.OPERATIONS_ADMIN
    )
    Membership.objects.create(
        tenant=setup.tenant, user=unassigned, role=TenantRole.STAFF_ACCOUNTANT
    )
    with tenant_context(setup.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        if grant_state != "role":
            capability = Capability.objects.get(slug="clients.view_all")
            RoleGrant.objects.filter(
                capability=capability, role=TenantRole.OPERATIONS_ADMIN
            ).update(level=GrantLevel.NONE)
            UserCapabilityGrant.objects.create(
                tenant=setup.tenant,
                user=operations,
                capability=capability,
                level=GrantLevel.FULL,
                expires_at=moment - timedelta(days=1)
                if grant_state == "expired"
                else moment + timedelta(days=1),
                revoked_at=moment if grant_state == "revoked" else None,
            )
        members = list(
            Membership.objects.filter(
                tenant=setup.tenant, client__isnull=True, is_active=True
            ).select_related("user")
        )
        expected = {
            member.pk
            for member in members
            if member.user_id == setup.staff.pk
            or portfolio_scope(member.user, tenant_id=setup.tenant.pk)
            is PortfolioScope.ALL
        }
        # When the display roster uses its single batched query.
        actual = {member.pk for member in _roster(setup.client.pk)}
    # Then it has the same ceiling and assignment narrowing, not just the same size.
    assert actual == expected
    assert {
        member.pk for member in members if member.user_id == setup.staff.pk
    } <= actual
    assert (
        not {member.pk for member in members if member.user_id == unassigned.pk}
        & actual
    )
    assert (
        next(member.pk for member in members if member.user_id == operations.pk)
        in actual
    ) is (grant_state in {"role", "raised"})

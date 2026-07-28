"""The portfolio scope may only ever narrow what the matrix already permits.

The first case is the important one. `FIRM_WIDE_ROLES` is a product rule sitting under
the published matrix, and the only way it can be wrong in a dangerous direction is by
naming a role the matrix does not grant `clients.view_all` — which would widen a
portfolio past its permission ceiling. Comparing the set against the seeded grants
makes that impossible to introduce silently.
"""

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.accounts.models import User
from apps.authz.models import Capability, GrantLevel, RoleGrant
from apps.authz.portfolio import (
    FIRM_WIDE_ROLES,
    VIEW_ALL,
    PortfolioScope,
    portfolio_scope,
    visible_clients,
)
from apps.core.tenancy import tenant_context
from apps.tenants.models import TenantRole
from tests.ui.factories import Firm, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def alpha() -> Firm:
    firm = make_firm("alpha-scope", client_count=3)
    assign(firm, firm.clients[0], firm.accountant)
    return firm


def test_no_firm_wide_role_exceeds_the_matrix() -> None:
    """The set may narrow the matrix. It must never reach past it."""
    capability = Capability.objects.get(slug=VIEW_ALL)
    permitted = set(
        RoleGrant.objects.filter(
            capability=capability,
            level=GrantLevel.FULL,
        ).values_list("role", flat=True),
    )
    assert permitted >= FIRM_WIDE_ROLES, (
        "a role here holds a firm-wide portfolio without holding clients.view_all"
    )


def test_the_set_is_a_strict_narrowing_and_not_a_copy() -> None:
    """If it equalled the matrix it would not be narrowing anything, and the staff
    accountant's assignment scope — which three acceptance criteria name — would be
    silently absent."""
    assert TenantRole.STAFF_ACCOUNTANT not in FIRM_WIDE_ROLES


def test_an_owner_works_the_whole_firm(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(alpha.owner) is PortfolioScope.ALL
        assert visible_clients(alpha.owner).count() == len(alpha.clients)


def test_a_staff_accountant_works_their_assignments(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(alpha.accountant) is PortfolioScope.ASSIGNED
        assert list(visible_clients(alpha.accountant)) == [alpha.clients[0]]


def test_an_anonymous_caller_gets_nothing(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(AnonymousUser()) is PortfolioScope.NONE
        assert list(visible_clients(AnonymousUser())) == []


def test_an_account_with_no_membership_here_gets_nothing(alpha: Firm) -> None:
    outsider = User.objects.create_user(
        email="fora@example.com",
        password="irrelevant-here",  # noqa: S106
    )
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(outsider) is PortfolioScope.NONE
        assert list(visible_clients(outsider)) == []


def test_visible_clients_is_always_a_queryset_never_none(alpha: Firm) -> None:
    """Callers compose further filters onto it; None would be an AttributeError at
    the worst possible moment, and a list would silently drop the tenant filter."""
    with tenant_context(alpha.tenant.id):
        for actor in (alpha.owner, alpha.accountant, AnonymousUser()):
            result = visible_clients(actor)
            assert hasattr(result, "filter")


def test_another_firms_clients_are_never_visible(alpha: Firm) -> None:
    beta = make_firm("beta-scope", client_count=2)
    with tenant_context(alpha.tenant.id):
        visible = set(visible_clients(alpha.owner).values_list("pk", flat=True))
    assert visible.isdisjoint({client.pk for client in beta.clients})

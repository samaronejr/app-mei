"""The bulk resolver must never be able to disagree with the single one.

`granted_levels` exists only to spare a navigation bar two queries per item. The
moment it answers something `resolve_level` would not, it stops being an optimisation
and becomes a second permission system — so the first case here walks the entire
published matrix, every capability against every role, and refuses any divergence.
"""

import pytest
from django.contrib.auth.models import AnonymousUser
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.accounts.models import User
from apps.authz.matrix import MATRIX
from apps.authz.models import GrantLevel
from apps.authz.services import UnknownCapability, granted_levels, resolve_level
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db

ALL_SLUGS = [row.slug for row in MATRIX]
# One capability existence check, one membership read, one grant read.
BULK_QUERY_BUDGET = 3


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha-bulk")


def _member(tenant: Tenant, role: str) -> User:
    user = User.objects.create_user(
        email=f"{role}@alpha-bulk.example.com",
        password="irrelevant-here",  # noqa: S106
    )
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return user


@pytest.mark.parametrize("role", [choice.value for choice in TenantRole])
def test_bulk_resolution_agrees_with_resolve_level(tenant: Tenant, role: str) -> None:
    user = _member(tenant, role)
    with tenant_context(tenant.id):
        bulk = granted_levels(user, ALL_SLUGS)
        one_at_a_time = {slug: resolve_level(user, slug) for slug in ALL_SLUGS}
    assert bulk == one_at_a_time


def test_it_resolves_the_whole_matrix_in_a_fixed_number_of_queries(
    tenant: Tenant,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    user = _member(tenant, TenantRole.OWNER)
    with tenant_context(tenant.id), django_assert_num_queries(BULK_QUERY_BUDGET):
        granted_levels(user, ALL_SLUGS)


def test_an_anonymous_caller_is_refused_everything(tenant: Tenant) -> None:
    with tenant_context(tenant.id):
        levels = granted_levels(AnonymousUser(), ALL_SLUGS)
    assert set(levels.values()) == {GrantLevel.NONE}


def test_an_account_with_no_membership_here_is_refused_everything(
    tenant: Tenant,
) -> None:
    outsider = User.objects.create_user(
        email="outsider@example.com",
        password="irrelevant-here",  # noqa: S106
    )
    with tenant_context(tenant.id):
        levels = granted_levels(outsider, ALL_SLUGS)
    assert set(levels.values()) == {GrantLevel.NONE}


def test_an_unknown_slug_raises_rather_than_reading_as_a_denial(
    tenant: Tenant,
) -> None:
    """A typo answering NONE is indistinguishable from a working refusal."""
    user = _member(tenant, TenantRole.OWNER)
    with tenant_context(tenant.id), pytest.raises(UnknownCapability):
        granted_levels(user, ["clients.view_all", "clients.view_evrything"])


def test_a_repeated_slug_is_resolved_once(tenant: Tenant) -> None:
    user = _member(tenant, TenantRole.OWNER)
    with tenant_context(tenant.id):
        levels = granted_levels(user, ["billing.manage", "billing.manage"])
    assert levels == {"billing.manage": GrantLevel.FULL}

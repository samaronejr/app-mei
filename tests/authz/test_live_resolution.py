"""Cheaper SQL must still resolve each caller's current answer independently."""

import pytest
from django.contrib.auth.models import AnonymousUser
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.accounts.models import User
from apps.authz.models import GrantLevel, RoleGrant
from apps.authz.services import (
    Actor,
    UnknownCapability,
    granted_levels,
    granted_levels_with_role,
    resolve_level,
)
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db
ACTION = "clients.view_all"


@pytest.fixture
def member() -> tuple[User, Tenant]:
    tenant = Tenant.objects.create(name="Live resolution", slug="live-resolution")
    user = User.objects.create_user(email="live-resolution@example.test")
    Membership.objects.create(tenant=tenant, user=user, role=TenantRole.OWNER)
    return user, tenant


def _answer(user: Actor, mode: str, action: str = ACTION) -> str:
    if mode == "single":
        return resolve_level(user, action)
    if mode == "bulk":
        return granted_levels(user, [action])[action]
    return granted_levels_with_role(user, [action])[1][action]


@pytest.mark.parametrize("mode", ["single", "bulk", "with_role"])
def test_each_resolution_is_one_live_query_and_sees_a_changed_grant(
    member: tuple[User, Tenant],
    mode: str,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    user, tenant = member
    with tenant_context(tenant.pk):
        with django_assert_num_queries(1):
            assert _answer(user, mode) == GrantLevel.FULL
        RoleGrant.objects.filter(role=TenantRole.OWNER, capability__slug=ACTION).update(
            level=GrantLevel.NONE
        )

        assert _answer(user, mode) == GrantLevel.NONE


@pytest.mark.parametrize("mode", ["single", "bulk", "with_role"])
@pytest.mark.parametrize("identity", ["anonymous", "superuser"])
def test_unknown_capability_precedes_identity_short_circuits(
    member: tuple[User, Tenant], mode: str, identity: str
) -> None:
    _, tenant = member
    user = (
        AnonymousUser()
        if identity == "anonymous"
        else User.objects.create_superuser(
            email="live-resolution-root@example.test", password=None
        )
    )
    with tenant_context(tenant.pk), pytest.raises(UnknownCapability):
        _answer(user, mode, "unknown.live-resolution")


def test_empty_capability_selection_still_reports_the_current_role(
    member: tuple[User, Tenant],
) -> None:
    user, tenant = member
    with tenant_context(tenant.pk):
        assert granted_levels_with_role(user, []) == (TenantRole.OWNER, {})

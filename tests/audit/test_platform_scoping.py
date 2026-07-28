"""The positive control for the six tables with no database-layer isolation.

`Tenant`, `Membership`, `Invite`, `AccessLog` and `PlatformEvent` are allow-listed in
`NON_TENANT_TABLES` for reasons that are individually sound — the middleware must read
memberships before any tenant context can exist; a login failure has no tenant. The
consequence is that for these tables the application layer is the ONLY layer, and an
allow-list entry protects nothing.

So each of them must answer `for_user()`, and each must answer it correctly. The
parametrized case below is the per-model access test: for every one of them, a member
of Alpha sees at least one of Alpha's rows and none of Beta's. Stated that way it
cannot pass on an empty result, which is what a broken filter produces.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import models

from apps.accounts.models import User
from apps.audit.models import AccessLog, AuditAction, PlatformEvent
from apps.core.access import PlatformScopedManager
from apps.tenants.models import Invite, Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db(transaction=True)


class Fixture:
    """Two firms, one member of each, and a row of every kind for both."""

    def __init__(self) -> None:
        self.alpha = Tenant.objects.create(name="Alpha", slug="alpha")
        self.beta = Tenant.objects.create(name="Beta", slug="beta")
        self.alpha_user = User.objects.create_user(email="a@alpha.example")
        self.beta_user = User.objects.create_user(email="b@beta.example")
        for tenant, user in (
            (self.alpha, self.alpha_user),
            (self.beta, self.beta_user),
        ):
            Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
            Invite.issue(
                tenant=tenant,
                email=f"convidado@{tenant.slug}.example",
                role=TenantRole.STAFF_ACCOUNTANT,
            )
            AccessLog.objects.create(
                tenant=tenant,
                user=user,
                method="GET",
                path=f"/{tenant.slug}/",
                status_code=200,
            )
            PlatformEvent.objects.create(
                tenant=tenant,
                actor=user,
                action=AuditAction.LOGIN_SUCCEEDED,
                subject=user.email,
            )


@pytest.fixture
def data() -> Fixture:
    return Fixture()


def _belongs_to(row: models.Model, tenant: Tenant) -> bool:
    if isinstance(row, Tenant):
        return row.pk == tenant.pk
    return bool(getattr(row, "tenant_id", None) == tenant.pk)


SCOPED_MODELS: dict[str, Callable[[], PlatformScopedManager[Any]]] = {
    "Tenant": lambda: Tenant.objects,
    "Membership": lambda: Membership.objects,
    "Invite": lambda: Invite.objects,
    "AccessLog": lambda: AccessLog.objects,
    "PlatformEvent": lambda: PlatformEvent.objects,
}


@pytest.mark.parametrize("name", list(SCOPED_MODELS))
def test_for_user_returns_only_the_callers_own_firms_rows(
    name: str,
    data: Fixture,
) -> None:
    # Given rows of this kind for both firms
    manager = SCOPED_MODELS[name]()

    # When a member of Alpha asks for what they may see
    visible = list(manager.for_user(data.alpha_user))

    # Then they see at least one of Alpha's rows...
    assert visible, f"{name}.for_user returned nothing — an empty result proves nothing"
    assert any(_belongs_to(row, data.alpha) for row in visible)

    # ...and none of Beta's. The unfiltered manager holds both, which is what makes
    # this assertion meaningful rather than a restatement of the fixture.
    assert not any(_belongs_to(row, data.beta) for row in visible)
    assert manager.count() > len(visible)


@pytest.mark.parametrize("name", list(SCOPED_MODELS))
def test_for_user_is_fail_closed_for_anonymous_callers(
    name: str,
    data: Fixture,
) -> None:
    # Given the same rows and no session
    manager = SCOPED_MODELS[name]()

    # When an anonymous caller asks
    # Then nothing is returned, matching what row-level security would do if these
    # tables could carry a policy at all
    assert manager.for_user(AnonymousUser()).count() == 0
    assert manager.count() > 0
    assert data.alpha.pk != data.beta.pk


@pytest.mark.parametrize("name", list(SCOPED_MODELS))
def test_for_user_does_not_widen_for_a_superuser(name: str, data: Fixture) -> None:
    # Given a platform superuser with no membership anywhere
    root = User.objects.create_superuser(email="root@platform.example")
    manager = SCOPED_MODELS[name]()

    # When they ask through the ordinary scoped path
    # Then they get nothing. Cross-tenant reach is a separate, audited, break-glass
    # path — never a silent widening of a query every screen uses.
    assert manager.for_user(root).count() == 0
    assert data.alpha.pk != data.beta.pk


def test_an_inactive_membership_grants_no_visibility(data: Fixture) -> None:
    # Given a member of Alpha whose membership was revoked
    Membership.objects.filter(user=data.alpha_user).update(is_active=False)

    # When they ask what they may see
    # Then the answer is nothing — deactivation is enforced, not merely recorded
    assert Tenant.objects.for_user(data.alpha_user).count() == 0
    assert AccessLog.objects.for_user(data.alpha_user).count() == 0

"""A `UserCapabilityGrant` moves one account above its role, and back below it.

The matrix itself cannot express "this one staff accountant may also approve" — a
sixth role is the wrong shape for it, and S8.1 names the right one: a per-user,
per-capability row inside the firm, resolvable to FULL and revocable back. This
module specifies the four properties that make the row trustworthy:

* granting RAISES `resolve_level` for that user and no other mechanic does — the
  role grant underneath stays NONE, which is the whole point of `actions.approve`
  being a capability rather than a role;
* revoking LOWERS it again, and the revocation is visible to the very next request —
  no cache, stash or session may carry the old answer forward;
* a grant in another tenant is **invisible**, not merely inactive — the row sits
  behind the same fail-closed policy every tenant table carries;
* a client-role membership can never be raised by a user grant, whatever row is
  written — the portal stash answers from role levels alone.

The team-page toggle that administers the grant is covered here too, because the
toggle is the grant's public face: it exists only for `staff_accountant` rows, only
for callers holding `users.create`, and every flip is audited.

`apps.authz.grants` is imported inside a helper rather than at module top: the
module did not exist when this test was written, and a module-level import would
have made the whole FILE a collection error instead of the failing tests a
missing service deserves.
"""

from http import HTTPStatus
from types import ModuleType
from typing import Final

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.authz.models import (
    Capability,
    GrantLevel,
    Role,
    RoleGrant,
    UserCapabilityGrant,
)
from apps.authz.services import can, granted_levels, resolve_level, role_of
from apps.core.tenancy import client_context, tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.ui.factories import (
    Firm,
    add_client,
    add_member,
    client_base,
    make_firm,
)

pytestmark = pytest.mark.django_db(transaction=True)

SLUG: Final = "acme-grant"
OTHER_SLUG: Final = "beta-grant"
REVIEWER_CAPABILITY: Final = "actions.approve"
# The gated surface the grant is proven against. Registered in tests/core/urls.py,
# gated `require_can("actions.approve")` under `cycles_r1`: owner and ops hold it
# FULL by role, staff_accountant holds it NONE until a grant exists — which makes
# the endpoint a direct instrument for reading `resolve_level` end to end.
APPROVE_ROUTE: Final = "r1-approve"
GRANT_TOGGLE_ROUTE: Final = "member-review-toggle"

GRANTED: Final = ("CAPABILITY_GRANTED", "capability_granted")
REVOKED: Final = ("CAPABILITY_REVOKED", "capability_revoked")


def _grants() -> ModuleType:
    """Import the grant service inside the test, so its absence fails a test."""
    from apps.authz import grants  # noqa: PLC0415

    return grants


def _new_action(named: tuple[str, str]) -> str:
    """Resolve one audit action, saying what is missing rather than aliasing it."""
    name, value = named
    member = getattr(AuditAction, name, None)
    assert member is not None, (
        f"apps/audit/models.py declares no AuditAction.{name}; add it rather than "
        f"logging grants under a different action"
    )
    assert str(member) == value
    return str(member)


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    settings.ROOT_URLCONF = "tests.core.urls"
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "tests.portal.urls")


class Cast:
    """Two firms; the first holds a second staff member to prove grants are narrow."""

    def __init__(self) -> None:
        self.firm: Firm = make_firm(SLUG)
        self.tenant: Tenant = self.firm.tenant
        self.accountant: User = self.firm.accountant
        self.colleague: User = add_member(
            self.tenant,
            f"colega@{SLUG}.example.com",
            TenantRole.STAFF_ACCOUNTANT,
        )
        self.ops: User = add_member(
            self.tenant,
            f"ops@{SLUG}.example.com",
            TenantRole.OPERATIONS_ADMIN,
        )
        self.client = add_client(
            self.firm,
            legal_name="Acme Grant Cliente MEI",
            base=client_base(0, SLUG),
        )
        self.portal: User = add_member(
            self.tenant,
            f"portal@{SLUG}.example.com",
            TenantRole.CLIENT_OWNER,
            client=self.client,
        )
        self.other: Firm = make_firm(OTHER_SLUG)


@pytest.fixture
def cast() -> Cast:
    return Cast()


def _membership(cast: Cast, user: User) -> Membership:
    return Membership.objects.get(user=user, tenant=cast.tenant, client__isnull=True)


def _enable_r1(tenant: Tenant) -> None:
    tenant.features = [*tenant.features, "cycles_r1"]
    tenant.save(update_fields=["features"])


def _approve(cast: Cast, user: User) -> int:
    http = Client()
    http.force_login(user)
    return http.get(
        reverse(APPROVE_ROUTE),
        headers={"host": f"{cast.tenant.slug}.localhost"},
    ).status_code


def _capability() -> Capability:
    return Capability.objects.get(slug=REVIEWER_CAPABILITY)


def _role_level(role: str, capability: str) -> str:
    """Read the role's own level for `capability`, the baseline a grant overrides."""
    grant = RoleGrant.objects.get(role=role, capability__slug=capability)
    return str(grant.level)


def _grant(cast: Cast, user: User) -> UserCapabilityGrant:
    """Write one grant row inside the firm's tenant context."""
    return UserCapabilityGrant.objects.create(
        tenant=cast.tenant,
        user=user,
        capability=_capability(),
        level=GrantLevel.FULL,
        granted_by=cast.firm.owner,
    )


# ----------------------------------------------------------------- raise / lower


def test_an_active_grant_raises_the_level_above_the_role(cast: Cast) -> None:
    # Given a staff accountant, whose role grant on actions.approve is NONE
    assert _role_level(Role.STAFF_ACCOUNTANT, REVIEWER_CAPABILITY) == GrantLevel.NONE

    # When a grant is written under the firm's context
    with tenant_context(cast.tenant.id):
        _grant(cast, cast.accountant)
        level = resolve_level(cast.accountant, REVIEWER_CAPABILITY)
        admitted = can(cast.accountant, REVIEWER_CAPABILITY)

    # Then the grant, not the role, is what opens the gate — and only FULL does
    assert level == GrantLevel.FULL
    assert admitted is True


def test_a_revoked_grant_lowers_the_level_back_to_the_role(cast: Cast) -> None:
    # Given an accountant whose grant has been stood down
    with tenant_context(cast.tenant.id):
        grant = _grant(cast, cast.accountant)
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_at"])

        # When the level is resolved
        # Then the role's own NONE is the answer again — an inactive row is a record
        # that permission existed, never a source of it
        assert resolve_level(cast.accountant, REVIEWER_CAPABILITY) == GrantLevel.NONE
        assert can(cast.accountant, REVIEWER_CAPABILITY) is False


def test_a_grant_raises_no_other_member_of_the_firm(cast: Cast) -> None:
    # Given a grant on ONE staff accountant — the firm holds a second one precisely
    # so "only the named account" is assertable rather than implied
    with tenant_context(cast.tenant.id):
        _grant(cast, cast.accountant)

        # When the colleague's level is resolved for the same capability
        # Then the role's NONE is still the whole answer: a per-user grant does not
        # widen the role, which is what keeps it cheaper than a sixth role
        assert resolve_level(cast.colleague, REVIEWER_CAPABILITY) == GrantLevel.NONE


def test_a_second_active_grant_is_impossible_at_the_database(cast: Cast) -> None:
    # Given an accountant already holding a live grant
    with tenant_context(cast.tenant.id):
        _grant(cast, cast.accountant)

        # When a second active row is attempted for the same (tenant, user,
        # capability) — the shape two racing POSTs would produce if the service
        # read-then-wrote without serialising on anything. The inner atomic() is
        # a savepoint, not decoration: the IntegrityError poisons every statement
        # after it until a rollback boundary, and the assertions below need the
        # enclosing transaction alive.
        # Then the partial unique index refuses it outright: the constraint, not
        # caller care, is what makes "one live grant" a fact rather than a habit
        with transaction.atomic(), pytest.raises(IntegrityError):
            _grant(cast, cast.accountant)

        # And the already-revoked row does NOT collide — the index is partial on
        # purpose, so a grant can be re-issued after a revoke
        first = UserCapabilityGrant.objects.get(user=cast.accountant)
        first.revoked_at = timezone.now()
        first.save(update_fields=["revoked_at"])
        _grant(cast, cast.accountant)
        assert (
            UserCapabilityGrant.objects.filter(
                user=cast.accountant,
                revoked_at__isnull=True,
            ).count()
            == 1
        )


# --------------------------------------------------------------- tenancy and RLS


def test_a_grant_in_another_tenant_is_invisible_to_this_one(cast: Cast) -> None:
    # Given a grant written INSIDE the other firm's context — the row exists, and it
    # belongs to beta, not to acme
    with tenant_context(cast.other.tenant.id):
        UserCapabilityGrant.objects.create(
            tenant=cast.other.tenant,
            user=cast.accountant,
            capability=_capability(),
            level=GrantLevel.FULL,
            granted_by=cast.other.owner,
        )
        assert UserCapabilityGrant.objects.count() == 1, (
            "the fixture grant was never written"
        )

    # When acme's context is live, the row is not merely filtered by the ORM: the
    # unscoped manager — RLS the only layer left — still cannot see it
    with tenant_context(cast.tenant.id):
        # ALL_OBJECTS_OK: measuring the database boundary itself. .objects would
        # apply the ORM filter on top of RLS and a zero count would prove half of
        # what this test exists to pin.
        assert UserCapabilityGrant.all_objects.count() == 0

        # And the resolution path cannot raise the accountant on it either
        assert resolve_level(cast.accountant, REVIEWER_CAPABILITY) == GrantLevel.NONE

    # And under the owning tenant the row is still there — invisibility, not loss
    with tenant_context(cast.other.tenant.id):
        assert UserCapabilityGrant.objects.count() == 1


def test_a_revoked_grant_fails_at_the_next_request(cast: Cast) -> None:
    # Given the accountant CAN reach the gated endpoint while the grant is live
    _enable_r1(cast.tenant)
    membership = _membership(cast, cast.accountant)
    with tenant_context(cast.tenant.id):
        _grants().set_review_capability(
            actor=cast.firm.owner,
            membership=membership,
            enabled=True,
        )
    assert _approve(cast, cast.accountant) == HTTPStatus.OK, (
        "the grant did not reach the endpoint — a revoke test against a 403 would "
        "prove nothing"
    )

    # When the grant is revoked and the SAME session asks again
    with tenant_context(cast.tenant.id):
        _grants().set_review_capability(
            actor=cast.firm.owner,
            membership=membership,
            enabled=False,
        )
    status = _approve(cast, cast.accountant)

    # Then the answer is the role's own: no stash, cache or session carries the old
    # grant into the next request
    assert status == HTTPStatus.FORBIDDEN


def test_bulk_resolution_cannot_diverge_from_resolve_level_for_grants(
    cast: Cast,
) -> None:
    # Given a granted accountant — `granted_levels` feeds navigation and the portal
    # stash, so a path that forgot the grant table there would show screens `can()`
    # refuses, or refuse screens `can()` admits
    with tenant_context(cast.tenant.id):
        _grant(cast, cast.accountant)
        slugs = [REVIEWER_CAPABILITY, "clients.view_all"]
        bulk = granted_levels(cast.accountant, slugs)
        single = {slug: resolve_level(cast.accountant, slug) for slug in slugs}

    # Then both answerers see the grant
    assert bulk == single
    assert bulk[REVIEWER_CAPABILITY] == GrantLevel.FULL


def test_a_grant_written_onto_a_client_role_changes_nothing(cast: Cast) -> None:
    # Given a hostile-by-construction row: the ORM will happily store a grant naming
    # a portal member, so resolution itself must be the boundary
    with tenant_context(cast.tenant.id):
        _grant(cast, cast.portal)

        # When the level is resolved — under no client context, the portal
        # membership is invisible to `role_of`, so the grant must not smuggle a
        # firm-side answer in on its own
        assert resolve_level(cast.portal, REVIEWER_CAPABILITY) == GrantLevel.NONE
        assert can(cast.portal, REVIEWER_CAPABILITY) is False

    # And inside the portal's own client context, where the client role DOES
    # resolve — the row must still not reach it: user grants answer firm-side
    # questions only, which is what keeps the portal stash honest
    with tenant_context(cast.tenant.id), client_context(cast.client.id):
        assert role_of(cast.portal) == TenantRole.CLIENT_OWNER
        assert resolve_level(cast.portal, REVIEWER_CAPABILITY) == GrantLevel.NONE
        assert can(cast.portal, REVIEWER_CAPABILITY) is False

    # And the domain path refuses to mint one too, not merely to honour it
    with (
        tenant_context(cast.tenant.id),
        pytest.raises(_grants().GrantSeatError),
    ):
        _grants().set_review_capability(
            actor=cast.firm.owner,
            membership=Membership.objects.get(
                user=cast.portal,
                tenant=cast.tenant,
                client__isnull=False,
            ),
            enabled=True,
        )


# ---------------------------------------------------------- the team-page toggle


def _toggle_url(membership: Membership) -> str:
    return reverse(GRANT_TOGGLE_ROUTE, args=[membership.pk])


def _active_grant(cast: Cast, user: User) -> UserCapabilityGrant | None:
    with tenant_context(cast.tenant.id):
        return UserCapabilityGrant.objects.filter(
            user=user,
            capability=_capability(),
            revoked_at__isnull=True,
        ).first()


def _audit_rows(cast: Cast, action: str) -> int:
    with tenant_context(cast.tenant.id):
        return Event.objects.filter(action=action, tenant_id=cast.tenant.id).count()


def test_the_owner_turns_review_on_and_off_with_audit_rows(cast: Cast) -> None:
    # Given the staff member's firm-side row and both audit actions resolved
    granted = _new_action(GRANTED)
    revoked = _new_action(REVOKED)
    membership = _membership(cast, cast.accountant)
    http = cast.firm.as_owner()

    # When the owner grants review
    response = http.post(_toggle_url(membership), {"enabled": "1"})

    # Then the accountant holds an active FULL grant, audited once
    assert response.status_code == HTTPStatus.FOUND
    row = _active_grant(cast, cast.accountant)
    assert row is not None
    assert row.level == GrantLevel.FULL
    assert row.granted_by_id == cast.firm.owner.pk
    assert _audit_rows(cast, granted) == 1

    # And when the owner takes it back
    response = http.post(_toggle_url(membership), {"enabled": "0"})

    # Then no active grant remains, the row still records who had it, and the
    # revocation is its own audit event
    assert response.status_code == HTTPStatus.FOUND
    assert _active_grant(cast, cast.accountant) is None
    with tenant_context(cast.tenant.id):
        assert UserCapabilityGrant.objects.filter(user=cast.accountant).count() == 1
    assert _audit_rows(cast, revoked) == 1


def test_the_toggle_is_idempotent_under_repeated_posts(cast: Cast) -> None:
    # Given an already-granted accountant
    membership = _membership(cast, cast.accountant)
    http = cast.firm.as_owner()
    http.post(_toggle_url(membership), {"enabled": "1"})
    assert _audit_rows(cast, _new_action(GRANTED)) == 1

    # When the same instruction arrives again — a double-click, a retry
    response = http.post(_toggle_url(membership), {"enabled": "1"})

    # Then one active row exists and no second event was written: a retry must not
    # become a second fact
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(cast.tenant.id):
        assert (
            UserCapabilityGrant.objects.filter(
                user=cast.accountant,
                revoked_at__isnull=True,
            ).count()
            == 1
        )
    assert _audit_rows(cast, _new_action(GRANTED)) == 1


def test_a_staff_accountant_cannot_toggle_review_for_anyone(cast: Cast) -> None:
    # Given a caller whose role holds users.create at NONE
    http = cast.firm.as_accountant()

    # When they post the toggle — on a colleague's row, and on their own
    for target in (
        _membership(cast, cast.colleague),
        _membership(cast, cast.accountant),
    ):
        response = http.post(_toggle_url(target), {"enabled": "1"})
        assert response.status_code == HTTPStatus.FORBIDDEN

    # Then nobody holds a grant
    assert _active_grant(cast, cast.colleague) is None
    assert _active_grant(cast, cast.accountant) is None


@pytest.mark.parametrize("member", ["ops", "portal"])
def test_the_toggle_only_exists_for_staff_rows(cast: Cast, member: str) -> None:
    # Given a membership whose role is not staff_accountant — the ops admin already
    # holds the capability FULL, and the portal row must never reach the endpoint
    membership = (
        _membership(cast, cast.ops)
        if member == "ops"
        else Membership.objects.get(
            user=cast.portal,
            tenant=cast.tenant,
            client__isnull=False,
        )
    )
    http = cast.firm.as_owner()

    # When its pk is posted to the toggle
    response = http.post(_toggle_url(membership), {"enabled": "1"})

    # Then the answer is 404, matching `_visible_membership`'s contract: a control
    # that does not exist for a row answers as if the route did not exist for it,
    # rather than confirming the row is real
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert _active_grant(cast, getattr(cast, member)) is None


def test_the_toggle_cannot_reach_another_firms_membership(cast: Cast) -> None:
    # Given the other firm's owner row, complete and valid — in somebody else's firm
    stranger = Membership.objects.get(user=cast.other.owner, tenant=cast.other.tenant)
    http = cast.firm.as_owner()

    # When this firm's owner posts its pk
    response = http.post(_toggle_url(stranger), {"enabled": "1"})

    # Then it is not found — `for_user` is the only scoping these rows have
    assert response.status_code == HTTPStatus.NOT_FOUND
    with tenant_context(cast.other.tenant.id):
        assert UserCapabilityGrant.objects.count() == 0


def test_the_service_refuses_the_grant_without_the_capability(cast: Cast) -> None:
    # Given a staff accountant calling the DOMAIN path directly, with no view
    # decorator in the way — the re-check here is what keeps a management command
    # or a future API from minting grants the HTTP gate would refuse
    membership = _membership(cast, cast.colleague)

    with (
        tenant_context(cast.tenant.id),
        pytest.raises(_grants().GrantNotPermittedError),
    ):
        _grants().set_review_capability(
            actor=cast.accountant,
            membership=membership,
            enabled=True,
        )

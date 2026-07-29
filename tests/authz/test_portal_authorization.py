"""A portal role resolves through the same `can()` as every firm role.

The design constraint is one authorization path, not two. `role_of` gained a single
filter on the client context and `_is_attached_to` gained a single branch; there is no
portal-specific permission function, because a second one would be a second matrix.

Two things here are load-bearing and easy to get wrong:

* Outside a portal request `current_client_id` is None, which reads as `client=None` —
  the firm-side sentinel. So Celery tasks and management commands, which never set a
  client, keep resolving firm memberships exactly as before.
* `limited` is genuinely reachable for a client role: `client_collaborator` holds it on
  `invoices.issue` and `reports.view_financial`. Without the portal branch those fall
  through to `ClientAssignment`, which records which STAFF ACCOUNTANT covers a client —
  a MEI collaborator appears in it for nobody, so they would be denied their own data.
"""

import pytest

from apps.accounts.models import User
from apps.authz.models import GrantLevel
from apps.authz.services import _is_attached_to, can, resolve_level, role_of
from apps.clients.models import ClientAssignment, ClientCompany
from apps.core.tenancy import client_context, tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db

PORTAL_LIMITED_CAPABILITY = "invoices.issue"
PORTAL_FULL_CAPABILITY = "documents.transfer"
FIRM_LIMITED_CAPABILITY = "audit.view"


class Fixture:
    """One firm, two of its MEI clients, a staff accountant and a portal user."""

    def __init__(
        self,
        tenant: Tenant,
        alpha: ClientCompany,
        beta: ClientCompany,
        staff: User,
        portal: User,
    ) -> None:
        self.tenant = tenant
        self.alpha = alpha
        self.beta = beta
        self.staff = staff
        self.portal = portal


@pytest.fixture
def firm() -> Fixture:
    tenant = Tenant.objects.create(name="Firma", slug="firma-authz")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding; the denials below need both clients present.
        alpha = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )
        # ALL_OBJECTS_OK: the sibling a portal user must never reach.
        beta = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE BETA",
            cnpj="11444777000161",
            is_mei=True,
        )
    staff = User.objects.create_user(
        email="staff@firma-authz.example.com",
        password="irrelevant-here",  # noqa: S106
    )
    portal = User.objects.create_user(
        email="mei@firma-authz.example.com",
        password="irrelevant-here",  # noqa: S106
    )
    Membership.objects.create(
        user=staff,
        tenant=tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
    )
    Membership.objects.create(
        user=portal,
        tenant=tenant,
        role=TenantRole.CLIENT_COLLABORATOR,
        client=alpha,
    )
    with tenant_context(tenant.id):
        ClientAssignment.objects.create(tenant=tenant, client=alpha, user=staff)
    return Fixture(tenant, alpha, beta, staff, portal)


def test_the_portal_role_resolves_inside_a_client_context(firm: Fixture) -> None:
    # Given a portal user scoped to alpha
    with tenant_context(firm.tenant.id), client_context(firm.alpha.id):
        # When their role is resolved
        role = role_of(firm.portal)

    # Then it is the client role, found through the same lookup firm roles use
    assert role == TenantRole.CLIENT_COLLABORATOR


def test_the_portal_role_is_invisible_without_a_client_context(firm: Fixture) -> None:
    # Given the same user, with no client established
    with tenant_context(firm.tenant.id):
        # When their role is resolved, the filter reads client=None
        role = role_of(firm.portal)

    # Then nothing is found. A portal identity is not a firm identity, so a request
    # that never established a client cannot borrow one.
    assert role is None


def test_the_firm_role_is_unchanged_by_the_client_filter(firm: Fixture) -> None:
    # Given a staff accountant and no client context, as every task and command runs
    with tenant_context(firm.tenant.id):
        role = role_of(firm.staff)

    # Then the firm-side membership resolves exactly as it did before T-060
    assert role == TenantRole.STAFF_ACCOUNTANT


def test_a_portal_user_is_attached_to_its_own_client(firm: Fixture) -> None:
    # Given a capability where a client role resolves to `limited`
    with tenant_context(firm.tenant.id), client_context(firm.alpha.id):
        level = resolve_level(firm.portal, PORTAL_LIMITED_CAPABILITY)
        allowed = can(firm.portal, PORTAL_LIMITED_CAPABILITY, firm.alpha)

    # Then the level is limited and the object refinement lets its own client through
    assert level == GrantLevel.LIMITED
    assert allowed is True


def test_a_portal_user_is_not_attached_to_a_sibling_client(firm: Fixture) -> None:
    # Given the same portal session
    with tenant_context(firm.tenant.id), client_context(firm.alpha.id):
        allowed = can(firm.portal, PORTAL_LIMITED_CAPABILITY, firm.beta)

    # Then the sibling is refused at the application layer too, not only by the
    # restrictive policy in the database
    assert allowed is False


def test_a_portal_user_reaches_a_full_capability_on_its_own_client(
    firm: Fixture,
) -> None:
    # Given a capability granted `full` to the client role
    with tenant_context(firm.tenant.id), client_context(firm.alpha.id):
        allowed = can(firm.portal, PORTAL_FULL_CAPABILITY, firm.alpha)

    # Then no object refinement is needed and it passes
    assert allowed is True


def test_the_staff_accountant_limited_path_still_consults_assignments(
    firm: Fixture,
) -> None:
    # Given a firm capability that resolves to `limited`, with no client context
    with tenant_context(firm.tenant.id):
        assigned = can(firm.staff, FIRM_LIMITED_CAPABILITY, firm.alpha)
        unassigned = can(firm.staff, FIRM_LIMITED_CAPABILITY, firm.beta)

    # Then attachment is still decided by ClientAssignment, unchanged by T-060
    assert assigned is True
    assert unassigned is False


def test_a_firm_user_inside_a_client_context_is_denied(firm: Fixture) -> None:
    # Given the exact call that succeeds on the firm side, where staff IS assigned
    with tenant_context(firm.tenant.id):
        outside = can(firm.staff, FIRM_LIMITED_CAPABILITY, firm.alpha)

    # When the same call is made inside a client context
    with tenant_context(firm.tenant.id), client_context(firm.alpha.id):
        inside = can(firm.staff, FIRM_LIMITED_CAPABILITY, firm.alpha)

    # Then it flips to denied, because role_of filtered on client and found no
    # firm-side membership. This is the invariant that makes _is_attached_to's portal
    # branch safe while it ignores its `user` argument: nothing reaches that branch
    # without a membership over this client already proven. A "firm user views the
    # portal as the client" feature would relax that filter and turn the branch into
    # the decision, at which point it must start checking `user`.
    assert outside is True
    assert inside is False


def test_the_portal_branch_alone_would_admit_a_stranger(firm: Fixture) -> None:
    # Given a user with no membership anywhere
    stranger = User.objects.create_user(
        email="stranger@example.com",
        password="irrelevant-here",  # noqa: S106
    )

    # When the portal branch is consulted directly, bypassing role_of
    with tenant_context(firm.tenant.id), client_context(firm.alpha.id):
        attached = _is_attached_to(stranger, firm.alpha)
        denied_through_can = can(stranger, PORTAL_LIMITED_CAPABILITY, firm.alpha)

    # Then it says yes on its own and can() still says no. This pins the coupling
    # rather than the comment describing it: the primitive is permissive by
    # construction and role_of is the only thing holding it closed.
    #
    # If _is_attached_to is ever hardened to check `user`, the first assertion here
    # SHOULD fail — flip it to False and keep the second.
    assert attached is True
    assert denied_through_can is False


def test_the_portal_branch_is_reachable_rather_than_dead_code(firm: Fixture) -> None:
    # Given the published matrix
    with tenant_context(firm.tenant.id), client_context(firm.alpha.id):
        level = resolve_level(firm.portal, PORTAL_LIMITED_CAPABILITY)

    # Then a client role really does resolve to `limited` somewhere, which is what
    # makes the portal branch of _is_attached_to live code. If the matrix ever stops
    # granting `limited` to any client role, this fails and the branch should be
    # re-examined rather than left in place unexercised.
    assert level == GrantLevel.LIMITED

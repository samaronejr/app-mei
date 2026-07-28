"""Every level of the matrix, resolved through the one entry point.

The two cases worth reading closely are the `limited` pair. A staff accountant holds
`limited` on `clients.edit_tax_profile`... no — an operations admin does, and that is
the point: the level is a property of the role, and whether it *grants* anything is a
property of the object. Those two tests are the same call with a different object and
opposite answers, which is the behaviour a role-only decorator cannot express.
"""

from typing import Final

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template

from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.authz.models import Capability, GrantLevel, Role, RoleGrant
from apps.authz.services import UnknownCapability, can, resolve_level
from apps.clients.models import ClientCompany
from apps.clients.services import assign_client
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db

# One capability per level, chosen from the seeded matrix.
FULL_FOR_OWNER: Final[str] = "billing.manage"
NONE_FOR_ACCOUNTANT: Final[str] = "billing.manage"
LIMITED_FOR_OPS: Final[str] = "clients.edit_tax_profile"
LIMITED_FOR_ACCOUNTANT: Final[str] = "audit.view"
VIEW_CONFIRM_FOR_CLIENT_OWNER: Final[str] = "das.generate"
INITIATE_APPROVE_FOR_CLIENT_OWNER: Final[str] = "integrations.connect"
OWN_FOR_CLIENT_OWNER: Final[str] = "audit.view"
TICKET_ONLY_FOR_CLIENT_OWNER: Final[str] = "support.access"


class Firm:
    """One firm, its three assignable roles, and two clients."""

    def __init__(self) -> None:
        self.tenant = Tenant.objects.create(name="Alpha", slug="alpha")
        self.owner = self._member("owner@alpha.example.com", TenantRole.OWNER)
        self.accountant = self._member(
            "staff@alpha.example.com",
            TenantRole.STAFF_ACCOUNTANT,
        )
        self.ops = self._member("ops@alpha.example.com", TenantRole.OPERATIONS_ADMIN)
        self.assigned = self._client("Assigned Ltda", "AAAAAAAAAAAA01")
        self.unassigned = self._client("Unassigned Ltda", "AAAAAAAAAAAA02")

    def _member(self, email: str, role: str) -> User:
        user = User.objects.create_user(email=email, password="irrelevant-here")  # noqa: S106
        Membership.objects.create(user=user, tenant=self.tenant, role=role)
        return user

    def _client(self, legal_name: str, cnpj: str) -> ClientCompany:
        with tenant_context(self.tenant.id):
            return ClientCompany.objects.create(
                tenant=self.tenant,
                legal_name=legal_name,
                cnpj=cnpj,
            )


@pytest.fixture
def firm() -> Firm:
    return Firm()


def test_an_unknown_capability_raises_rather_than_denying(firm: Firm) -> None:
    # Given any account
    # When a slug the matrix does not define is asked about
    # Then it raises. Returning False would make a typo indistinguishable from a
    # working denial, hiding a permanently broken gate behind a plausible 403.
    with (
        tenant_context(firm.tenant.id),
        pytest.raises(UnknownCapability, match=r"nonexistent\.capability"),
    ):
        can(firm.owner, "nonexistent.capability")


def test_full_grants_the_action_outright(firm: Firm) -> None:
    # Given a firm owner, whom the matrix scores ✅ on billing
    # When the gate is asked
    with tenant_context(firm.tenant.id):
        # Then it opens, and the raw level says why
        assert resolve_level(firm.owner, FULL_FOR_OWNER) == GrantLevel.FULL
        assert can(firm.owner, FULL_FOR_OWNER) is True


def test_none_denies_the_action(firm: Firm) -> None:
    # Given a staff accountant, whom the matrix scores ❌ on billing
    # When the gate is asked
    with tenant_context(firm.tenant.id):
        # Then it stays shut
        assert resolve_level(firm.accountant, NONE_FOR_ACCOUNTANT) == GrantLevel.NONE
        assert can(firm.accountant, NONE_FOR_ACCOUNTANT) is False


def test_limited_opens_only_for_an_assigned_client(firm: Firm) -> None:
    # Given an operations admin, whom the matrix scores Limited on the tax profile,
    # assigned to one of the firm's two clients
    with tenant_context(firm.tenant.id):
        assign_client(actor=firm.owner, client=firm.assigned, user=firm.ops)

        # When the gate is asked about each client in turn
        allowed = can(firm.ops, LIMITED_FOR_OPS, firm.assigned)
        refused = can(firm.ops, LIMITED_FOR_OPS, firm.unassigned)

        # Then the same role and the same capability answer differently per object,
        # which is exactly what a role-only decorator could not have expressed
        assert resolve_level(firm.ops, LIMITED_FOR_OPS) == GrantLevel.LIMITED
        assert allowed is True
        assert refused is False


def test_limited_without_an_object_fails_closed(firm: Firm) -> None:
    # Given the same Limited grant
    with tenant_context(firm.tenant.id):
        assign_client(actor=firm.owner, client=firm.assigned, user=firm.ops)

        # When the gate is asked with no object at all
        # Then it refuses. A Limited grant cannot be decided without the thing it is
        # limited to, and the safe answer to an undecidable question is no.
        assert can(firm.ops, LIMITED_FOR_OPS) is False


def test_a_staff_accountant_is_denied_an_unassigned_client(firm: Firm) -> None:
    # Given a staff accountant with Limited on the audit log
    with tenant_context(firm.tenant.id):
        assign_client(actor=firm.owner, client=firm.assigned, user=firm.accountant)

        # When both clients are checked
        # Then only the assigned one is reachable
        assert can(firm.accountant, LIMITED_FOR_ACCOUNTANT, firm.assigned) is True
        assert can(firm.accountant, LIMITED_FOR_ACCOUNTANT, firm.unassigned) is False


def test_own_opens_only_for_the_actors_own_record(firm: Firm) -> None:
    # Given a role holding "Own actions only" on the audit log. The matrix scores that
    # level for the client roles, which have no assignment path before the Phase 2
    # portal, so the grant is moved onto an assignable role by editing the ROW — which
    # is also the acceptance criterion that changing a level needs no code change.
    RoleGrant.objects.filter(
        role=Role.STAFF_ACCOUNTANT,
        capability=Capability.objects.get(slug=OWN_FOR_CLIENT_OWNER),
    ).update(level=GrantLevel.OWN)

    mine = PlatformEvent(action=AuditAction.LOGIN_SUCCEEDED, actor=firm.accountant)
    theirs = PlatformEvent(action=AuditAction.LOGIN_SUCCEEDED, actor=firm.ops)

    # When the same account asks about its own record and about somebody else's
    with tenant_context(firm.tenant.id):
        level = resolve_level(firm.accountant, OWN_FOR_CLIENT_OWNER)
        own_record = can(firm.accountant, OWN_FOR_CLIENT_OWNER, mine)
        other_record = can(firm.accountant, OWN_FOR_CLIENT_OWNER, theirs)
        no_record = can(firm.accountant, OWN_FOR_CLIENT_OWNER)

    # Then identity decides it, and no view code changed to make that happen
    assert level == GrantLevel.OWN
    assert own_record is True
    assert other_record is False
    assert no_record is False


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        (VIEW_CONFIRM_FOR_CLIENT_OWNER, GrantLevel.VIEW_CONFIRM),
        (INITIATE_APPROVE_FOR_CLIENT_OWNER, GrantLevel.INITIATE_APPROVE),
        (OWN_FOR_CLIENT_OWNER, GrantLevel.OWN),
        (TICKET_ONLY_FOR_CLIENT_OWNER, GrantLevel.TICKET_ONLY),
    ],
)
def test_the_channel_levels_do_not_grant_the_action_itself(
    firm: Firm,
    capability: str,
    expected: str,
) -> None:
    # Given a role that holds a restricted variant rather than the action
    grant = RoleGrant.objects.get(
        role=Role.CLIENT_OWNER,
        capability=Capability.objects.get(slug=capability),
    )

    # When the seeded level is read
    # Then it is the restricted one, and it does not open the plain action. Collapsing
    # these into a boolean True would grant the full workflow to someone the matrix
    # only lets confirm, request, or open a ticket about it.
    assert grant.level == expected
    assert grant.level not in {GrantLevel.FULL, GrantLevel.NONE}


def test_a_superuser_short_circuits_before_the_membership_lookup(firm: Firm) -> None:
    # Given a platform administrator, who holds no Membership anywhere — the report's
    # platform-admin column has no assignable role behind it
    admin = User.objects.create_superuser(
        email="root@platform.example.com",
        password="irrelevant-here",  # noqa: S106
    )
    assert not Membership.objects.filter(user=admin).exists()

    # When a capability only that column grants is asked about
    with tenant_context(firm.tenant.id):
        allowed = can(admin, "tenants.create")

    # Then it opens. A membership-first resolution order would have denied the
    # platform administrator every capability the matrix grants them.
    assert allowed is True
    assert can(admin, "tenants.create", firm.assigned) is True


def test_an_anonymous_caller_is_denied(firm: Firm) -> None:
    # Given no account at all
    # When the gate is asked
    with tenant_context(firm.tenant.id):
        # Then it refuses, matching what row-level security would do
        assert can(AnonymousUser(), FULL_FOR_OWNER) is False


def test_no_tenant_context_fails_closed(firm: Firm) -> None:
    # Given a firm owner but no resolved tenant
    # When the gate is asked outside any tenant context
    # Then it refuses rather than guessing which firm was meant
    assert can(firm.owner, FULL_FOR_OWNER) is False


def test_an_explicit_tenant_overrides_the_ambient_one(firm: Firm) -> None:
    # Given no ambient context
    # When the tenant is named explicitly, as a domain function handed one does
    allowed = can(firm.owner, FULL_FOR_OWNER, tenant_id=firm.tenant.pk)

    # Then the check resolves against that firm
    assert allowed is True


def test_the_template_tag_answers_the_same_question(firm: Firm) -> None:
    # Given a template gating a control on a capability
    template = Template(
        "{% load authz %}"
        "{% can user 'billing.manage' as allowed %}"
        "{% if allowed %}SHOWN{% else %}HIDDEN{% endif %}",
    )

    # When it is rendered for each role
    with tenant_context(firm.tenant.id):
        for_owner = template.render(Context({"user": firm.owner}))
        for_accountant = template.render(Context({"user": firm.accountant}))

    # Then the screen hides what the gate would refuse
    assert for_owner == "SHOWN"
    assert for_accountant == "HIDDEN"

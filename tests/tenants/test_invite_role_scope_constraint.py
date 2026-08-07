"""The database refuses an invitation whose role and client scope disagree.

W7 turned `invite_role_is_firm_side` from a one-armed rule (`role IN FIRM_ROLES`) into
the same two-armed rule `membership_role_matches_client_scope` already held: a firm role
names no client, a client role names one. The sibling module
`test_membership_role_scope_constraint.py` is this file one step later in the story,
and the repetition is deliberate — an invitation is a PROMISE of a membership, so the
promise has to be refused at the moment the firm makes it rather than at the moment the
invitee tries to keep it. By then the token is in a stranger's hands, they present a
link that was always doomed, and the firm is not in the room.

The widened arm is the reason this file exists at all. `frozenset` membership inside a
`CheckConstraint` is written once and read never, and the T-064 mutation matrix already
demonstrated on the sibling constraint that a CHECK nobody exercises can be deleted with
the whole suite staying green. Every case below therefore goes through PostgreSQL and
asserts `IntegrityError`, never through a form or a `full_clean()`.

The last case covers the composite foreign key the same migration adds. It is not the
CHECK — it is the other half of "an invitation names a client": WHICH client, and
whether that client belongs to the firm doing the inviting.
"""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import INVITE_VALIDITY, Invite, Tenant, TenantRole

pytestmark = pytest.mark.django_db


def make_client(tenant: Tenant, *, cnpj: str) -> ClientCompany:
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding for a constraint test.
        return ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE",
            cnpj=cnpj,
            is_mei=True,
        )


def write_invite(
    *,
    tenant: Tenant,
    role: str,
    client: ClientCompany | None,
    seed: str,
) -> Invite:
    """Insert an invitation with no factory in the way.

    `Invite.issue` takes no client and generates its own token, so routing these cases
    through it would test the factory's signature rather than the table's constraint —
    and the constraint is the whole subject. A raw `create` is what a data migration, a
    shell session or a future issue path all reduce to.
    """
    return Invite.objects.create(
        tenant=tenant,
        client=client,
        email="convidado@example.com",
        role=role,
        token=Invite.hash_token(seed),
        expires_at=timezone.now() + INVITE_VALIDITY,
    )


@pytest.fixture
def firm() -> tuple[Tenant, ClientCompany]:
    tenant = Tenant.objects.create(name="Firma", slug="firma-invite-constraint")
    return tenant, make_client(tenant, cnpj="11222333000181")


def test_a_client_role_without_a_client_is_refused(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given a client-side role on an invitation that names no client
    tenant, _client = firm

    # When it is written
    # Then the database refuses it. This is the dangerous direction: accepting it would
    # write Membership(role=client_owner, client=None), which `role_of` resolves for a
    # FIRM-side session — a portal seat that got out of its client and became firm-wide.
    with pytest.raises(IntegrityError), transaction.atomic():
        write_invite(
            tenant=tenant,
            role=TenantRole.CLIENT_OWNER,
            client=None,
            seed="client-role-no-client",
        )


def test_a_firm_role_carrying_a_client_is_refused(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given a firm-side role on an invitation pointed at one client
    tenant, client = firm

    # When it is written
    # Then the database refuses it too. The mirror hazard: a staff accountant confined
    # to a single client, which the portal policies would then read as a portal identity
    # while the firm screens read it as a colleague.
    with pytest.raises(IntegrityError), transaction.atomic():
        write_invite(
            tenant=tenant,
            role=TenantRole.STAFF_ACCOUNTANT,
            client=client,
            seed="firm-role-with-client",
        )


def test_the_other_client_role_is_bound_by_the_same_arm(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given the second client role, which `CLIENT_ROLES` also names
    tenant, _client = firm

    # When it is written without a client
    # Then it is refused as well, so the arm is a set membership rather than one value
    # that happens to be spelled in the constraint.
    with pytest.raises(IntegrityError), transaction.atomic():
        write_invite(
            tenant=tenant,
            role=TenantRole.CLIENT_COLLABORATOR,
            client=None,
            seed="collaborator-no-client",
        )


def test_both_correct_shapes_are_accepted(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given the two shapes the widened constraint exists to permit
    tenant, client = firm

    # When each is written
    firm_side = write_invite(
        tenant=tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
        seed="firm-side-ok",
    )
    portal_side = write_invite(
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=client,
        seed="portal-side-ok",
    )

    # Then neither is refused. Without this the three refusals above would pass just as
    # loudly against a constraint that rejects every row, and the widening — the only
    # thing W7 actually changed — would be untested by the tests written for it.
    assert firm_side.client_id is None
    assert portal_side.client_id == client.pk


def test_an_invitation_cannot_name_another_firms_client(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given a client that belongs to an entirely different firm
    tenant, _client = firm
    stranger = Tenant.objects.create(name="Outra", slug="outra-invite-constraint")
    stranger_client = make_client(stranger, cnpj="11444777000161")

    # When this firm issues a portal invitation pointed at it. Django's own foreign key
    # checks `client_id` alone, and PostgreSQL evaluates referential integrity with row
    # security BYPASSED, so the single-column key proves only that SOME firm owns it.
    # `tenants_invite` carries no policy either, so nothing else is watching.
    # Then `invite_tenant_client_fk` refuses the row: the pair (tenant_id, client_id)
    # has to exist together in the registry.
    with pytest.raises(IntegrityError), transaction.atomic():
        write_invite(
            tenant=tenant,
            role=TenantRole.CLIENT_OWNER,
            client=stranger_client,
            seed="cross-tenant-client",
        )

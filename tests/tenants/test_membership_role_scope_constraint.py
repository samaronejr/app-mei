"""The database refuses a membership whose role and client scope disagree.

`membership_role_matches_client_scope` is what makes "a firm role has no client, a
client role names one" true of the DATA rather than merely of the code that writes it.
Every guard elsewhere in this suite polices queries; this constraint polices rows, and
it is the only thing standing between a bad write and a membership that is neither
firm-side nor portal.

It had no automated test until the T-064 mutation matrix dropped it and the whole suite
stayed green — the plan expected "T-059 failure QA red", but that QA is a recorded
evidence file, not a test that runs. By the matrix's own rule, a control whose removal
changes nothing is decoration. This is that rule applied to itself.
"""

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole


def make_user(email: str) -> User:
    return User.objects.create_user(email=email, password="irrelevant-here")  # noqa: S106


pytestmark = pytest.mark.django_db


@pytest.fixture
def firm() -> tuple[Tenant, ClientCompany]:
    tenant = Tenant.objects.create(name="Firma", slug="firma-constraint")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding for a constraint test.
        client = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE",
            cnpj="11222333000181",
            is_mei=True,
        )
    return tenant, client


def test_a_firm_role_carrying_a_client_is_refused(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given a firm-side role pointed at a client
    tenant, client = firm

    # When it is written
    # Then the database refuses it. Such a row would be a staff accountant scoped to one
    # client, which the portal policies would then treat as a portal identity.
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(
            user=make_user("firm-with-client@example.com"),
            tenant=tenant,
            role=TenantRole.STAFF_ACCOUNTANT,
            client=client,
        )


def test_a_client_role_without_a_client_is_refused(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given a client role with no client
    tenant, _client = firm

    # When it is written
    # Then the database refuses it. This is the dangerous direction: `role_of` filters
    # on `client=None` outside a portal request, so such a row would resolve a CLIENT
    # role for a FIRM-side session.
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(
            user=make_user("client-no-client@example.com"),
            tenant=tenant,
            role=TenantRole.CLIENT_OWNER,
            client=None,
        )


def test_both_correct_shapes_are_accepted(
    firm: tuple[Tenant, ClientCompany],
) -> None:
    # Given the two shapes the constraint exists to permit
    tenant, client = firm

    # Then neither is refused, so the tests above cannot be passing because the table
    # rejects everything.
    Membership.objects.create(
        user=make_user("firm-side@example.com"),
        tenant=tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
    )
    Membership.objects.create(
        user=make_user("portal-side@example.com"),
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=client,
    )

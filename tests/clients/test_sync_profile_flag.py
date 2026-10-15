"""`sync_profile_flag`: the dual-write service the admin and the import call.

The service is thin by design — everything load-bearing is delegated to
`correct_tax_profile`, so the cases that matter here are the DELEGATION'S
contract: the revision the caller's snapshot carries is honoured (a stale one is
refused before any row moves), and the client is resolved through the actor's
portfolio (an unassigned staff accountant cannot reach the timeline at all, and
no row is written on the way out).
"""

import pytest
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    TaxProfile,
    TaxRegime,
)
from apps.clients.services import (
    TaxProfileConflictError,
    profile_at,
    sync_profile_flag,
)
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def firm() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Firma Sync", slug="sync")
    owner = User.objects.create_user(email="dono@sync.test")
    staff = User.objects.create_user(email="contador@sync.test")
    Membership.objects.create(user=owner, tenant=tenant, role=TenantRole.OWNER)
    Membership.objects.create(
        user=staff, tenant=tenant, role=TenantRole.STAFF_ACCOUNTANT
    )
    return tenant, owner, staff


def _client(tenant: Tenant, *, is_mei: bool) -> ClientCompany:
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Cliente Sync",
            cnpj="12ABC34501DE35",
            is_mei=is_mei,
        )


def test_a_second_writer_on_a_stale_snapshot_is_refused_and_writes_nothing(
    firm: tuple[Tenant, User, User],
) -> None:
    tenant, owner, _staff = firm
    client = _client(tenant, is_mei=True)

    with tenant_context(tenant.id):
        # Given two callers holding the SAME pre-write snapshot: both read rev 1
        first_snapshot = ClientCompany.objects.get(pk=client.pk)
        second_snapshot = ClientCompany.objects.get(pk=client.pk)
        assert (
            first_snapshot.tax_profile_revision == second_snapshot.tax_profile_revision
        )

        # When the first caller's flag claim lands
        sync_profile_flag(actor=owner, client=first_snapshot, created=True)
        assert profile_at(client, timezone.localdate()).regime == TaxRegime.MEI  # type: ignore[union-attr]

        # Then the second caller — whose snapshot predates the bump — is refused
        # before a single profile row is touched
        before = TaxProfile.objects.count()
        second_snapshot.is_mei = False
        with pytest.raises(TaxProfileConflictError):
            sync_profile_flag(actor=owner, client=second_snapshot, created=False)
        assert TaxProfile.objects.count() == before


def test_an_unassigned_accountant_cannot_reach_the_timeline(
    firm: tuple[Tenant, User, User],
) -> None:
    tenant, _owner, staff = firm
    client = _client(tenant, is_mei=True)
    with tenant_context(tenant.id):
        assert not ClientAssignment.objects.filter(
            user=staff, client=client
        ).exists(), "the denial below must be about visibility, not a grant"

        # When an accountant with no assignment on this client tries to sync
        before = TaxProfile.objects.count()
        with pytest.raises(Http404):
            sync_profile_flag(actor=staff, client=client, created=False)

        # Then nothing was written — the 404 precedes every INSERT
        assert TaxProfile.objects.count() == before


def test_an_assigned_accountant_reaches_the_timeline(
    firm: tuple[Tenant, User, User],
) -> None:
    tenant, _owner, staff = firm
    client = _client(tenant, is_mei=False)
    with tenant_context(tenant.id):
        ClientAssignment.objects.create(
            tenant=tenant,
            client=client,
            user=staff,
            role=AssignmentRole.PRIMARY,
        )
        # staff holds FULL on clients.edit_tax_profile; only the portfolio narrows
        # them, and the assignment is exactly what widens it back
        claim = sync_profile_flag(actor=staff, client=client, created=True)
        assert claim is not None
        assert claim.regime == TaxRegime.UNKNOWN
        assert claim.recorded_by_id == staff.pk


def test_a_sync_that_repeats_todays_answer_writes_nothing(
    firm: tuple[Tenant, User, User],
) -> None:
    tenant, owner, _staff = firm
    client = _client(tenant, is_mei=True)
    with tenant_context(tenant.id):
        first = sync_profile_flag(actor=owner, client=client, created=True)
        assert first is not None
        client.refresh_from_db()

        # Re-saving with the flag unchanged must not restate the claim
        repeat = sync_profile_flag(actor=owner, client=client, created=False)
        assert repeat is None
        assert TaxProfile.objects.count() == 1

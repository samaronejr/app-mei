"""The tax-profile timeline: non-overlap enforced, corrections preserved.

Invariant 5's enforcement layer. A current (not superseded) `TaxProfile` claims a
regime for a half-open validity range; two current claims for one client may never
cover the same day — the `btree_gist` exclusion constraint on
`daterange(valid_from, valid_to, '[)')` makes that structural rather than
application-policed — and a correction supersedes and splits instead of rewriting
the row it replaces, so what the firm *believed* stays queryable after it learns
better.
"""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.clients.models import ClientCompany, TaxProfile
from apps.clients.services import correct_tax_profile, profile_at
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

INFINITY = date(9999, 12, 31)


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Escritorio Delta", slug="delta")


@pytest.fixture
def client(tenant: Tenant) -> ClientCompany:
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Mercearia Delta MEI",
            cnpj="12ABC34501DE35",
        )


@pytest.fixture
def actor(tenant: Tenant) -> User:
    # The service re-checks clients.edit_tax_profile inside the correction
    # transaction, so the actor needs a firm-side membership granting FULL on the
    # capability — a bare user cannot correct anything. The tenancy root is
    # unpoliced, so the membership lands without a tenant context.
    # No password: the suite exercises the service layer, never a login.
    user = User.objects.create_user(email="contadora@delta.test")
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    return user


def _current_ranges() -> list[tuple[date, date, str]]:
    """Every still-believed claim in the tenant, as (from, to, regime) triples."""
    rows = TaxProfile.objects.filter(superseded_at__isnull=True).order_by("valid_from")
    return [(row.valid_from, row.valid_to or INFINITY, str(row.regime)) for row in rows]


def _regime_at(client: ClientCompany, on: date) -> str:
    profile = profile_at(client, on)
    assert profile is not None, f"expected a claim covering {on}"
    return str(profile.regime)


def test_overlapping_current_ranges_are_rejected(
    tenant: Tenant,
    client: ClientCompany,
    actor: User,
) -> None:
    # Given a client whose current regime is MEI from 2025-01-01, open-ended
    assert_isolated_role()
    with tenant_context(tenant.id):
        correct_tax_profile(
            client=client,
            regime="mei",
            valid_from=date(2025, 1, 1),
            actor=actor,
        )

    # When a second CURRENT claim overlaps it — even partially — the insert is
    # refused by the exclusion constraint, not by application code. The failed
    # statement is wrapped in its own atomic block so the error unwinds to the
    # savepoint and the enclosing tenant_context stays usable.
    with tenant_context(tenant.id):
        with (
            pytest.raises(IntegrityError, match="taxprofile_current_range_excl"),
            transaction.atomic(),
        ):
            TaxProfile.objects.create(
                tenant_id=tenant.id,
                client=client,
                regime="simples",
                valid_from=date(2025, 6, 1),
                valid_to=date(2025, 12, 31),
                provenance="probe",
            )
        assert _current_ranges() == [(date(2025, 1, 1), INFINITY, "mei")]


def test_disjoint_ranges_and_other_clients_are_untouched(
    tenant: Tenant,
    client: ClientCompany,
    actor: User,
) -> None:
    # Given a current MEI claim on one client
    assert_isolated_role()
    with tenant_context(tenant.id):
        correct_tax_profile(
            client=client,
            regime="mei",
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 6, 1),
            actor=actor,
        )
        other = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Segunda LTDA",
            cnpj="33XYZ98765QR00",
        )
        # Then a range beginning exactly when it ends, and an overlapping range on a
        # DIFFERENT client, are both admitted — the [) bound and the client key are
        # part of the constraint, not conventions.
        TaxProfile.objects.create(
            tenant_id=tenant.id,
            client=client,
            regime="simples",
            valid_from=date(2025, 6, 1),
            provenance="probe",
        )
        TaxProfile.objects.create(
            tenant_id=tenant.id,
            client=other,
            regime="simples",
            valid_from=date(2025, 3, 1),
            provenance="probe",
        )
        assert len(_current_ranges()) == 3


def test_a_retrospective_correction_supersedes_and_splits(
    tenant: Tenant,
    client: ClientCompany,
    actor: User,
) -> None:
    # Given the believed timeline: MEI through 2025, then Simples open-ended
    assert_isolated_role()
    with tenant_context(tenant.id):
        correct_tax_profile(
            client=client,
            regime="mei",
            valid_from=date(2025, 1, 1),
            valid_to=date(2026, 1, 1),
            actor=actor,
        )
        correct_tax_profile(
            client=client,
            regime="simples",
            valid_from=date(2026, 1, 1),
            actor=actor,
        )

        # When a correction lands for May-September 2025, inside the first claim
        corrected = correct_tax_profile(
            client=client,
            regime="real",
            valid_from=date(2025, 5, 1),
            valid_to=date(2025, 10, 1),
            actor=actor,
            note="Receita confirmou desenquadramento retroativo",
        )

        # Then the overruled row is superseded but still readable...
        assert corrected.correction_of_id is not None
        original = TaxProfile.objects.get(pk=corrected.correction_of_id)
        assert original.superseded_at is not None
        assert original.regime == "mei"
        assert original.valid_to == date(2026, 1, 1)

        # ...its two surviving slices were re-asserted as current rows...
        assert _current_ranges() == [
            (date(2025, 1, 1), date(2025, 5, 1), "mei"),
            (date(2025, 5, 1), date(2025, 10, 1), "real"),
            (date(2025, 10, 1), date(2026, 1, 1), "mei"),
            (date(2026, 1, 1), INFINITY, "simples"),
        ]

        # ...and every row in the answer knows which claim it continues or overrules.
        # The superseded original still carries valid_from=2025-01-01, so the lookup
        # is restricted to current rows — the history is readable precisely because
        # supersede stamps rather than rewrites.
        head = TaxProfile.objects.get(
            valid_from=date(2025, 1, 1), superseded_at__isnull=True
        )
        tail = TaxProfile.objects.get(
            valid_from=date(2025, 10, 1), superseded_at__isnull=True
        )
        assert head.correction_of_id == original.id
        assert tail.correction_of_id == original.id

        # The correction is one audited event naming every row it moved. Each
        # correct_tax_profile call writes one, so the lookup pins the row this
        # correction created, not merely the action.
        event = Event.objects.get(
            action=AuditAction.TAX_PROFILE_CORRECTED,
            object_id=str(corrected.pk),
        )
        assert event.metadata["superseded"] == [str(original.id)]


def test_profile_at_reads_the_timeline_across_a_transition(
    tenant: Tenant,
    client: ClientCompany,
    actor: User,
) -> None:
    # Given MEI through March and Simples from April, as current claims
    assert_isolated_role()
    with tenant_context(tenant.id):
        correct_tax_profile(
            client=client,
            regime="mei",
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 4, 1),
            actor=actor,
        )
        correct_tax_profile(
            client=client,
            regime="simples",
            valid_from=date(2025, 4, 1),
            actor=actor,
        )

        # Then profile_at answers per the VALID clock on each side of the boundary
        assert _regime_at(client, date(2025, 3, 31)) == "mei"
        assert _regime_at(client, date(2025, 4, 1)) == "simples"
        assert _regime_at(client, date(2099, 1, 1)) == "simples"


def test_a_correction_without_the_capability_is_denied(
    tenant: Tenant,
    client: ClientCompany,
) -> None:
    # Given an authenticated user with NO membership in the firm
    assert_isolated_role()
    outsider = User.objects.create_user(email="intruso@elsewhere.test")

    # When it calls the service directly (bypassing any view gate)
    with (
        tenant_context(tenant.id),
        pytest.raises(PermissionDenied),
    ):
        correct_tax_profile(
            client=client,
            regime="mei",
            valid_from=date(2025, 1, 1),
            actor=outsider,
        )

    # Then no claim and no event were left behind
    with tenant_context(tenant.id):
        assert TaxProfile.objects.count() == 0
        assert (
            Event.objects.filter(action=AuditAction.TAX_PROFILE_CORRECTED).count() == 0
        )


def test_an_unknown_period_answers_none_not_a_guess(
    tenant: Tenant,
    client: ClientCompany,
    actor: User,
) -> None:
    # Given a claim that only covers 2025
    assert_isolated_role()
    with tenant_context(tenant.id):
        correct_tax_profile(
            client=client,
            regime="mei",
            valid_from=date(2025, 1, 1),
            valid_to=date(2026, 1, 1),
            actor=actor,
        )

        # Then dates outside every current claim answer None: before the earliest
        # is unknown, and so is after the last claim ended. Inventing either is how
        # stale regimes reach calendars.
        assert profile_at(client, date(2024, 12, 31)) is None
        assert profile_at(client, date(2026, 1, 1)) is None

        # And a client nobody has ever profiled answers None, not an exception.
        newcomer = ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Sem Perfil LTDA",
            cnpj="99QQQ11122XX00",
        )
        assert profile_at(newcomer, date(2025, 6, 1)) is None

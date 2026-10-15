"""The obligation engine answers from the effective-dated profile, not the flag.

T6 moves two readers off `is_mei`/`mei_category` onto `clients_taxprofile`:

* `refresh_das_calendars` sweeps the clients whose *profile* is MEI at the
  competence month the run would start — so a flag the timeline has outgrown no
  longer generates MEI deadlines, and a retroactively corrected regime stops the
  calendar the flag would keep writing.
* `threshold_status` measures the year against the ceiling of the *claim covering
  1 January*, so a client whose caminhoneiro category is on the profile is read
  against its own limit even when the legacy column still says `common`.

Both are pinned against the fixture deliberately creating flag-and-timeline
DISAGREEMENT, which is the only state in which "reads the profile" is falsifiable.
The legacy column is the fallback while a client carries no claim at all — the
state imports and pre-backfill rows legitimately sit in — and that fallback is
asserted too, so removing it reds rather than drifting.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.clients.models import ClientCompany, TaxProfile, TaxRegime
from apps.core.tenancy import tenant_context
from apps.fiscal.mei import MEICategory
from apps.obligations.models import MonthlyRevenue, Obligation
from apps.obligations.tasks import refresh_das_calendars
from apps.obligations.threshold import ThresholdBand, threshold_status
from apps.tenants.models import Tenant
from tests.ui.factories import client_base, make_cnpj

pytestmark = pytest.mark.django_db(transaction=True)

COMMON_CEILING = Decimal("81000.00")
CAMINHONEIRO_CEILING = Decimal("251600.00")


def _tenant(slug: str) -> Tenant:
    return Tenant.objects.create(name=slug.title(), slug=slug)


def _client(
    tenant: Tenant,
    index: int,
    *,
    is_mei: bool,
    opened_on: dt.date | None = dt.date(2020, 3, 10),
    mei_category: str = MEICategory.COMMON,
) -> ClientCompany:
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name=f"{tenant.slug} cliente {index}",
            cnpj=make_cnpj(client_base(index, seed=tenant.slug)),
            opened_on=opened_on,
            is_mei=is_mei,
            mei_category=mei_category,
        )


def _claim(
    client: ClientCompany,
    *,
    regime: str,
    valid_from: dt.date,
    valid_to: dt.date | None = None,
    mei_category: str | None = None,
) -> None:
    with tenant_context(client.tenant_id):
        TaxProfile.objects.create(
            tenant_id=client.tenant_id,
            client=client,
            regime=regime,
            mei_category=mei_category,
            valid_from=valid_from,
            valid_to=valid_to,
            provenance="test",
        )


# ------------------------------------------------------------------ the sweep


def test_the_sweep_reads_the_regime_off_the_profile_not_the_flag() -> None:
    # Given a firm holding one flagged-but-no-longer-MEI client and one
    # unflagged-but-MEI one — the two disagreements only a profile read separates
    tenant = _tenant("sweep-prof")
    stale = _client(tenant, 0, is_mei=True)
    _claim(
        stale,
        regime=TaxRegime.SIMPLES,
        valid_from=dt.date(2026, 1, 1),
    )
    migrated = _client(tenant, 1, is_mei=False)
    _claim(
        migrated,
        regime=TaxRegime.MEI,
        valid_from=dt.date(2020, 1, 1),
    )

    # When the nightly sweep runs
    created = refresh_das_calendars()

    # Then the flags were not consulted: stale produced nothing and migrated got
    # its twelve months, exactly inverted from what `is_mei` would have produced
    assert created == 12
    with tenant_context(tenant.id):
        assert Obligation.objects.filter(client=stale).count() == 0
        assert Obligation.objects.filter(client=migrated).count() == 12


def test_the_sweep_falls_back_to_the_flag_only_while_the_timeline_is_silent() -> None:
    # Given a client carrying no claim at all — the honest unknown state
    tenant = _tenant("sweep-silent")
    unclaimed = _client(tenant, 0, is_mei=True)

    # When the sweep runs
    created = refresh_das_calendars()

    # Then the legacy flag still carries it: clients the backfill predates cannot
    # silently lose their calendar, and the fallback is the flag, not a default
    assert created == 12
    with tenant_context(tenant.id):
        assert Obligation.objects.filter(client=unclaimed).count() == 12


def test_a_claim_that_ends_before_the_window_stops_the_calendar() -> None:
    # Given a client the timeline says was MEI only until last month
    tenant = _tenant("sweep-ended")
    ended = _client(tenant, 0, is_mei=True)
    today = timezone.localdate()
    _claim(
        ended,
        regime=TaxRegime.MEI,
        valid_from=dt.date(2020, 1, 1),
        valid_to=today.replace(day=1),
    )

    # When the sweep runs
    created = refresh_das_calendars()

    # Then nothing is generated: the claim covering the competence month decides,
    # and a MEI period that closed is not an MEI competence
    assert created == 0
    with tenant_context(tenant.id):
        assert Obligation.objects.filter(client=ended).count() == 0


# -------------------------------------------------------------- the threshold


def test_the_ceiling_category_comes_from_the_claim_covering_the_year() -> None:
    # Given a caminhoneiro on the profile whose legacy column still says common —
    # the divergence only the profile read resolves correctly
    tenant = _tenant("thr-prof")
    trucker = _client(
        tenant,
        0,
        is_mei=True,
        mei_category=MEICategory.COMMON,
    )
    _claim(
        trucker,
        regime=TaxRegime.MEI,
        mei_category=MEICategory.CAMINHONEIRO,
        valid_from=dt.date(2020, 1, 1),
    )
    with tenant_context(tenant.id):
        MonthlyRevenue.objects.create(
            tenant_id=tenant.id,
            client=trucker,
            competence_month=dt.date(2026, 1, 1),
            gross_amount=Decimal("200000.00"),
        )

    # When 2026 is evaluated
    with tenant_context(tenant.id):
        result = threshold_status(trucker, 2026)

    # Then it is measured against the caminhoneiro ceiling. The legacy column alone
    # would report this compliant client at 247% and desenquadrado retroactively.
    assert result.ceiling == CAMINHONEIRO_CEILING
    assert result.band is ThresholdBand.OK


def test_the_threshold_falls_back_to_the_legacy_column_while_unclaimed() -> None:
    # Given a caminhoneiro the timeline says nothing about — same fallback the
    # sweep uses, and the same reason: un-backported rows must keep resolving
    tenant = _tenant("thr-silent")
    trucker = _client(
        tenant,
        0,
        is_mei=True,
        mei_category=MEICategory.CAMINHONEIRO,
    )
    with tenant_context(tenant.id):
        MonthlyRevenue.objects.create(
            tenant_id=tenant.id,
            client=trucker,
            competence_month=dt.date(2026, 1, 1),
            gross_amount=Decimal("200000.00"),
        )

    # When evaluated
    with tenant_context(tenant.id):
        result = threshold_status(trucker, 2026)

    # Then the column answers, exactly as it did before profiles existed
    assert result.ceiling == CAMINHONEIRO_CEILING
    assert result.band is ThresholdBand.OK


def test_a_non_mei_claim_measures_against_the_common_ceiling() -> None:
    # Given a client whose claim covering 1 January is Simples — a client the
    # MEI monitor no longer has a category for
    tenant = _tenant("thr-simples")
    moved = _client(tenant, 0, is_mei=True, mei_category=MEICategory.CAMINHONEIRO)
    _claim(
        moved,
        regime=TaxRegime.SIMPLES,
        valid_from=dt.date(2020, 1, 1),
    )

    # When evaluated against a January the client was already not-MEI
    with tenant_context(tenant.id):
        result = threshold_status(moved, 2026)

    # Then the claim wins over the legacy category too: a non-MEI claim carries no
    # MEI category, so the conservative common ceiling applies rather than the
    # stale caminhoneiro one the column still asserts
    assert result.ceiling == COMMON_CEILING

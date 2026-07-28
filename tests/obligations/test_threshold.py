"""The revenue threshold monitor, and the four legislated outcomes it tells apart.

**The 20% tolerance band is not cosmetic — it changes the legal outcome.** Resolução
CGSN nº 140/2018 art. 115 treats two overage cases very differently:

* Excess of 20% or less: the client stays MEI through 31 December and is desenquadrado
  effective **1 January of the following year**, settling the difference retroactively.
* Excess above 20%: desenquadramento is retroactive to **1 January of the current
  year** — except in the opening year, where it goes back to the client's **opening
  date** and the whole period is recomputed as ME.

That is FOUR outcomes, not two: {within tolerance, over tolerance} crossed with
{opening year, subsequent year}. Testing only the two subsequent-year cases leaves
the opening-year
over-tolerance branch — the one resolving to `opened_on` rather than 1 January —
entirely unexercised, and an implementation returning `date(year, 1, 1)` for every
over-tolerance case would pass. All four are asserted below.

**A partial month counts as a WHOLE month** (LC 123/2006 art. 18-A §2, restated
verbatim in PLP 186/2026 as "consideradas as frações de mês como um mês inteiro"). A
client opening on 20 July is measured over six months, July to December, not 5.4.
Computing fractional months is the obvious wrong implementation and it understates the
ceiling, which reports a compliant client as over the limit.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.fiscal.mei import MEICategory
from apps.obligations.models import FiscalParameter, MonthlyRevenue
from apps.obligations.threshold import (
    ThresholdBand,
    ThresholdStatus,
    threshold_status,
)
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db

PROJECT_ROOT = Path(__file__).resolve().parents[2]

COMMON_CEILING = Decimal("81000.00")
CAMINHONEIRO_CEILING = Decimal("251600.00")
OPENED_IN_JULY = date(2026, 7, 20)
# Six whole months, July through December, times R$ 6.750,00.
JULY_PROPORTIONAL_CEILING = Decimal("40500.00")


def make_client(
    slug: str,
    *,
    opened_on: date | None = None,
    category: str = MEICategory.COMMON,
) -> ClientCompany:
    """Create a tenant with one client, inside that tenant's own context."""
    tenant = Tenant.objects.create(name=slug.title(), slug=slug)
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name=f"{slug} MEI",
            cnpj="11222333000181",
            opened_on=opened_on,
            mei_category=category,
        )


def bill(
    client: ClientCompany, year: int, total: Decimal, *, from_month: int = 1
) -> None:
    """Record `total` as revenue, spread evenly across the months from `from_month`."""
    months = list(range(from_month, 13))
    each = (total / len(months)).quantize(Decimal("0.01"))
    remainder = total - each * (len(months) - 1)
    with tenant_context(client.tenant_id):
        for index, month in enumerate(months):
            MonthlyRevenue.objects.create(
                tenant_id=client.tenant_id,
                client=client,
                competence_month=date(year, month, 1),
                gross_amount=remainder if index == len(months) - 1 else each,
            )


def status_for(client: ClientCompany, year: int) -> ThresholdStatus:
    """Evaluate the monitor inside the client's own tenant context."""
    with tenant_context(client.tenant_id):
        return threshold_status(client, year)


def test_a_quiet_year_is_ok() -> None:
    # Given a long-established client billing half its ceiling
    client = make_client("thr-ok", opened_on=date(2020, 1, 15))
    bill(client, 2026, Decimal("40000.00"))

    # When the year is evaluated
    result = status_for(client, 2026)

    # Then it is comfortably inside the limit
    assert result.band is ThresholdBand.OK
    assert result.accumulated == Decimal("40000.00")
    assert result.ceiling == COMMON_CEILING
    assert result.desenquadramento_effective_on is None


def test_the_warning_band_opens_at_exactly_eighty_percent() -> None:
    # Given revenue of exactly 80% of the ceiling
    client = make_client("thr-80", opened_on=date(2020, 1, 15))
    bill(client, 2026, COMMON_CEILING * Decimal("0.80"))

    # When the year is evaluated
    result = status_for(client, 2026)

    # Then the boundary belongs to warning. An accountant needs the signal before the
    # limit is crossed, not at the moment it is.
    assert result.band is ThresholdBand.WARNING
    assert result.percentage_consumed == Decimal("80.00")
    assert result.desenquadramento_effective_on is None


def test_just_below_eighty_percent_is_still_ok() -> None:
    # Given revenue a centavo under the warning boundary
    client = make_client("thr-79", opened_on=date(2020, 1, 15))
    bill(client, 2026, COMMON_CEILING * Decimal("0.80") - Decimal("0.01"))

    # When the year is evaluated
    # Then it is ok. Without this the boundary test above would pass on an
    # implementation that warned about everything.
    assert status_for(client, 2026).band is ThresholdBand.OK


def test_revenue_exactly_at_the_ceiling_has_not_exceeded_it() -> None:
    # Given revenue of exactly the ceiling
    client = make_client("thr-100", opened_on=date(2020, 1, 15))
    bill(client, 2026, COMMON_CEILING)

    # When the year is evaluated
    result = status_for(client, 2026)

    # Then it is warning, not exceeded. Billing the limit is not exceeding it, and
    # reporting desenquadramento here would be a false alarm with legal weight.
    assert result.band is ThresholdBand.WARNING
    assert result.percentage_consumed == Decimal("100.00")
    assert result.desenquadramento_effective_on is None


def test_outcome_1_subsequent_year_within_tolerance() -> None:
    # Given an established client billing 110% of the ceiling — a 10% excess
    client = make_client("thr-o1", opened_on=date(2020, 1, 15))
    bill(client, 2026, COMMON_CEILING * Decimal("1.10"))

    # When 2026 is evaluated
    result = status_for(client, 2026)

    # Then the client stays MEI through 31 December and is desenquadrado effective
    # 1 January of the FOLLOWING year, settling the difference retroactively
    assert result.band is ThresholdBand.EXCEEDED_WITHIN_TOLERANCE
    assert result.desenquadramento_effective_on == date(2027, 1, 1)


def test_outcome_2_subsequent_year_over_tolerance() -> None:
    # Given an established client billing 125% of the ceiling — a 25% excess
    client = make_client("thr-o2", opened_on=date(2020, 1, 15))
    bill(client, 2026, COMMON_CEILING * Decimal("1.25"))

    # When 2026 is evaluated
    result = status_for(client, 2026)

    # Then desenquadramento is retroactive to 1 January of the CURRENT year and the
    # whole year is recomputed as ME — a materially different bill
    assert result.band is ThresholdBand.EXCEEDED_OVER_TOLERANCE
    assert result.desenquadramento_effective_on == date(2026, 1, 1)


def test_outcome_3_opening_year_within_tolerance() -> None:
    # Given a client that opened on 20 July 2026 and billed 110% of its PROPORTIONAL
    # ceiling of six months times R$ 6.750
    client = make_client("thr-o3", opened_on=OPENED_IN_JULY)
    bill(client, 2026, JULY_PROPORTIONAL_CEILING * Decimal("1.10"), from_month=7)

    # When 2026 is evaluated
    result = status_for(client, 2026)

    # Then it is measured against the proportional ceiling, not R$ 81.000, and the
    # within-tolerance rule still resolves to 1 January of the following year
    assert result.ceiling == JULY_PROPORTIONAL_CEILING
    assert result.months_counted == 6
    assert result.band is ThresholdBand.EXCEEDED_WITHIN_TOLERANCE
    assert result.desenquadramento_effective_on == date(2027, 1, 1)


def test_outcome_4_opening_year_over_tolerance_goes_back_to_the_opening_date() -> None:
    # Given the same opening-year client billing 125% of its proportional ceiling
    client = make_client("thr-o4", opened_on=OPENED_IN_JULY)
    bill(client, 2026, JULY_PROPORTIONAL_CEILING * Decimal("1.25"), from_month=7)

    # When 2026 is evaluated
    result = status_for(client, 2026)

    # Then desenquadramento is retroactive to the OPENING DATE, not to 1 January.
    # This is the branch the whole four-outcome requirement exists to protect: an
    # implementation returning date(year, 1, 1) for every over-tolerance case passes
    # outcomes 1 to 3 and fails only here.
    assert result.band is ThresholdBand.EXCEEDED_OVER_TOLERANCE
    assert result.desenquadramento_effective_on == OPENED_IN_JULY
    assert result.desenquadramento_effective_on != date(2026, 1, 1)


def test_the_proportional_ceiling_flags_excess_far_below_81000() -> None:
    # Given the opening-year client at 125% of its proportional ceiling
    client = make_client("thr-prop", opened_on=OPENED_IN_JULY)
    bill(client, 2026, JULY_PROPORTIONAL_CEILING * Decimal("1.25"), from_month=7)

    # When 2026 is evaluated
    result = status_for(client, 2026)

    # Then it is exceeded even though total revenue is barely half of R$ 81.000.
    # Measuring an opening year against the full annual ceiling would report this
    # client as comfortably compliant while Receita Federal desenquadra them.
    assert result.accumulated < COMMON_CEILING
    assert result.band is ThresholdBand.EXCEEDED_OVER_TOLERANCE


def test_a_partial_month_counts_as_a_whole_month() -> None:
    # Given two clients opening in the same month, one on the 1st and one on the 31st
    first = make_client("thr-m1", opened_on=date(2026, 7, 1))
    last = make_client("thr-m31", opened_on=date(2026, 7, 31))

    # When both are evaluated
    # Then both are measured over six whole months. Prorating by days would give the
    # second client a smaller ceiling for opening eleven hours later.
    assert status_for(first, 2026).months_counted == 6
    assert status_for(last, 2026).months_counted == 6
    assert status_for(first, 2026).ceiling == status_for(last, 2026).ceiling


def test_a_december_opening_is_measured_over_one_month() -> None:
    # Given a client that opened on 31 December
    client = make_client("thr-dec", opened_on=date(2026, 12, 31))

    # When the opening year is evaluated
    result = status_for(client, 2026)

    # Then exactly one month is counted, never zero. A zero would make the ceiling
    # zero and put every client that opened in December instantly over the limit.
    assert result.months_counted == 1
    assert result.ceiling == Decimal("6750.00")


def test_the_year_after_opening_uses_the_full_annual_ceiling() -> None:
    # Given the July 2026 client, evaluated for 2027
    client = make_client("thr-next", opened_on=OPENED_IN_JULY)
    bill(client, 2027, Decimal("50000.00"))

    # When 2027 is evaluated
    result = status_for(client, 2027)

    # Then the proportional rule is gone: it applies to the opening year alone
    assert result.is_opening_year is False
    assert result.ceiling == COMMON_CEILING
    assert result.months_counted == 12


def test_a_caminhoneiro_is_measured_against_its_own_ceiling() -> None:
    # Given a trucker billing R$ 200.000, which is far over the common ceiling
    client = make_client(
        "thr-truck",
        opened_on=date(2020, 1, 15),
        category=MEICategory.CAMINHONEIRO,
    )
    bill(client, 2026, Decimal("200000.00"))

    # When the year is evaluated
    result = status_for(client, 2026)

    # Then it is comfortably inside the caminhoneiro limit. Measured against the
    # common R$ 81.000 this client would be reported at 247% and desenquadrado
    # retroactively to January — a compliant client told to recompute a whole year.
    assert result.ceiling == CAMINHONEIRO_CEILING
    assert result.band is ThresholdBand.OK


def test_the_ceiling_comes_from_the_parameter_table_not_from_a_constant() -> None:
    # Given a client whose 2027 revenue is 100% of the CURRENT ceiling
    client = make_client("thr-param", opened_on=date(2020, 1, 15))
    bill(client, 2027, COMMON_CEILING)
    assert status_for(client, 2027).band is ThresholdBand.WARNING

    # When a hypothetical 2027 ceiling is INSERTED — no deploy, no code edit
    FiscalParameter.objects.create(
        key="mei.annual_ceiling",
        mei_category=MEICategory.COMMON,
        value="110000.00",
        valid_from=date(2027, 1, 1),
        source_note="hypothetical, for the no-deploy property",
        source_url="https://example.invalid/plp-186-2026",
    )

    # Then the same revenue is re-read against the new limit and the band changes.
    # This is the property the whole parameters-as-data design exists to provide.
    updated = status_for(client, 2027)
    assert updated.ceiling == Decimal("110000.00")
    assert updated.band is ThresholdBand.OK


def test_the_tolerance_rate_comes_from_the_parameter_table_too() -> None:
    # Given a client at 125% of the ceiling in 2027, which is over the seeded 20%
    client = make_client("thr-tol", opened_on=date(2020, 1, 15))
    bill(client, 2027, COMMON_CEILING * Decimal("1.25"))
    assert status_for(client, 2027).band is ThresholdBand.EXCEEDED_OVER_TOLERANCE

    # When a wider tolerance takes effect on 1 January 2027, inserted over the
    # open-ended 2026 row exactly as an enacted change would be
    FiscalParameter.objects.create(
        key="mei.excess_tolerance_pct",
        mei_category=None,
        value="0.30",
        valid_from=date(2027, 1, 1),
        source_note="hypothetical, proving the rate is not a constant",
        source_url="https://example.invalid/tolerance",
    )

    # Then the same revenue falls back inside tolerance, and the effective date moves
    # a full year with it. 2026 is untouched, because latest-wins is date-scoped.
    updated = status_for(client, 2027)
    assert updated.band is ThresholdBand.EXCEEDED_WITHIN_TOLERANCE
    assert updated.desenquadramento_effective_on == date(2028, 1, 1)


def test_the_tolerance_boundary_belongs_to_the_within_band() -> None:
    # Given revenue at exactly 120% — an excess of exactly 20%
    client = make_client("thr-120", opened_on=date(2020, 1, 15))
    bill(client, 2026, COMMON_CEILING * Decimal("1.20"))

    # When the year is evaluated
    # Then "excess up to 20%" includes 20% itself
    assert status_for(client, 2026).band is ThresholdBand.EXCEEDED_WITHIN_TOLERANCE


def test_a_centavo_past_the_tolerance_boundary_crosses_the_band() -> None:
    # Given revenue one centavo above 120% of the ceiling
    client = make_client("thr-120b", opened_on=date(2020, 1, 15))
    bill(client, 2026, COMMON_CEILING * Decimal("1.20") + Decimal("0.01"))

    # When the year is evaluated
    # Then the band flips, and with it the effective date moves back a whole year
    result = status_for(client, 2026)
    assert result.band is ThresholdBand.EXCEEDED_OVER_TOLERANCE
    assert result.desenquadramento_effective_on == date(2026, 1, 1)


def test_a_client_with_no_revenue_recorded_is_ok_and_not_an_error() -> None:
    # Given a client with no revenue rows at all
    client = make_client("thr-empty", opened_on=date(2020, 1, 15))

    # When the year is evaluated
    result = status_for(client, 2026)

    # Then it reports zero rather than raising or returning None
    assert result.accumulated == Decimal("0.00")
    assert result.percentage_consumed == Decimal("0.00")
    assert result.band is ThresholdBand.OK


def test_every_computed_value_is_a_decimal() -> None:
    # Given any evaluated client
    client = make_client("thr-dec-types", opened_on=date(2020, 1, 15))
    bill(client, 2026, Decimal("40000.00"))
    result = status_for(client, 2026)

    # When the returned values are type-checked
    # Then all three are Decimal. A float here cannot represent 0.20, and that
    # comparison decides whether desenquadramento is retroactive to January or to
    # the following year.
    assert isinstance(result.accumulated, Decimal)
    assert isinstance(result.ceiling, Decimal)
    assert isinstance(result.percentage_consumed, Decimal)


def test_no_float_appears_anywhere_in_the_computation_path() -> None:
    # Given the modules that compute the answer
    modules = [
        PROJECT_ROOT / "apps" / "obligations" / "threshold.py",
        PROJECT_ROOT / "apps" / "obligations" / "parameters.py",
    ]
    assert all(path.is_file() for path in modules)

    # When their source is scanned for float construction and coercion
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}:{number + 1}: {line.strip()}"
        for path in modules
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if "float(" in line or ": float" in line or "-> float" in line
    ]

    # Then there are none. A single float() in this path silently rounds money.
    assert offenders == [], "float in the money path:\n  " + "\n  ".join(offenders)

"""How much of the MEI ceiling a client has consumed, and what happens if it is past it.

**Reporting "over the limit" is not enough — the 20% tolerance changes the legal
outcome, and the two situations are a full year apart in consequence.** Resolução CGSN
nº 140/2018 art. 115:

* Excess of 20% or less: the client remains MEI through 31 December and is
  desenquadrado effective **1 January of the following year**, settling the difference
  retroactively.
* Excess above 20%: desenquadramento is retroactive to **1 January of the current
  year** — except in the opening year, where it reaches back to the **opening date**
  and the whole period is recomputed as ME.

Four outcomes, not two. A monitor that collapses them gives an accountant the same
signal for "start planning the migration for January" and "the year you already filed
is now wrong".

**A partial month counts as a whole month** (LC 123/2006 art. 18-A §2). A client
opening on 20 July is measured over six months, not 5.4. Prorating by days is the
obvious implementation, understates the ceiling, and reports compliant clients as over.

**Every number here is a Decimal read from `FiscalParameter`.** The ceiling, the
proportional monthly rate and the tolerance rate are all rows, so an enacted change is
an INSERT rather than a deploy — and binary floating point cannot represent 0.20, the
exact comparison that decides which of the four outcomes applies.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from django.db import models

from apps.clients.models import ClientCompany
from apps.obligations.models import MonthlyRevenue
from apps.obligations.parameters import parameter_for

CEILING_KEY = "mei.annual_ceiling"
PROPORTIONAL_KEY = "mei.monthly_proportional"
TOLERANCE_KEY = "mei.excess_tolerance_pct"

MONTHS_IN_YEAR = 12
WARNING_AT = Decimal("0.80")
CEILING_AT = Decimal("1.00")
CENTAVO = Decimal("0.01")


class ThresholdBand(StrEnum):
    """How much of the ceiling is consumed, in the four steps the law distinguishes."""

    OK = "ok"
    WARNING = "warning"
    EXCEEDED_WITHIN_TOLERANCE = "exceeded_within_tolerance"
    EXCEEDED_OVER_TOLERANCE = "exceeded_over_tolerance"


@dataclass(frozen=True)
class ThresholdStatus:
    """One client's position against its ceiling for one calendar year."""

    accumulated: Decimal
    ceiling: Decimal
    percentage_consumed: Decimal
    band: ThresholdBand
    months_counted: int
    is_opening_year: bool
    desenquadramento_effective_on: date | None


def _months_active_in(client: ClientCompany, year: int) -> int:
    """Return how many months of `year` this client was open for, counting whole months.

    "Consideradas as frações de mês como um mês inteiro": a company that opened on the
    31st was active in that month, so a December opening counts one month and never
    zero. Zero would make the proportional ceiling zero and put every client that
    opened in December instantly over the limit.
    """
    if client.opened_on is None or client.opened_on.year != year:
        return MONTHS_IN_YEAR
    return MONTHS_IN_YEAR - client.opened_on.month + 1


def _ceiling_for(client: ClientCompany, year: int, months: int) -> Decimal:
    # Resolved as of 1 January: the limit that governs a calendar year is the one in
    # force when that year began, so the answer does not change under a client
    # mid-year. An enacted increase carries valid_from = 1 January and is picked up.
    on_date = date(year, 1, 1)
    if months == MONTHS_IN_YEAR:
        return parameter_for(CEILING_KEY, on_date, client.mei_category)
    monthly = parameter_for(PROPORTIONAL_KEY, on_date, client.mei_category)
    return monthly * months


def _band_for(consumed: Decimal, tolerance: Decimal) -> ThresholdBand:
    # Compared on the RAW ratio, never on the rounded percentage. Quantizing first
    # would round 79.999% up to 80.00 and move a client into the warning band on a
    # display decision.
    if consumed > CEILING_AT + tolerance:
        return ThresholdBand.EXCEEDED_OVER_TOLERANCE
    if consumed > CEILING_AT:
        return ThresholdBand.EXCEEDED_WITHIN_TOLERANCE
    if consumed >= WARNING_AT:
        return ThresholdBand.WARNING
    return ThresholdBand.OK


def _effective_date_for(
    band: ThresholdBand,
    client: ClientCompany,
    year: int,
    *,
    is_opening_year: bool,
) -> date | None:
    if band is ThresholdBand.EXCEEDED_WITHIN_TOLERANCE:
        return date(year + 1, 1, 1)
    if band is not ThresholdBand.EXCEEDED_OVER_TOLERANCE:
        return None
    if is_opening_year and client.opened_on is not None:
        # The branch the four-outcome requirement exists to protect. Returning
        # 1 January here would be wrong by up to eleven months, and every other
        # outcome would still look right.
        return client.opened_on
    return date(year, 1, 1)


def threshold_status(client: ClientCompany, year: int) -> ThresholdStatus:
    """Evaluate this client's revenue for `year` against the ceiling that applies to it.

    Must be called inside the client's own tenant context: the revenue rows are
    tenant-scoped and would otherwise sum to zero under the fail-closed policy.
    """
    months = _months_active_in(client, year)
    is_opening_year = months != MONTHS_IN_YEAR
    ceiling = _ceiling_for(client, year, months)

    accumulated = MonthlyRevenue.objects.filter(
        client=client,
        competence_month__year=year,
    ).aggregate(
        total=models.Sum("gross_amount", default=Decimal("0.00")),
    )["total"]

    consumed = accumulated / ceiling
    tolerance = parameter_for(TOLERANCE_KEY, date(year, 1, 1), client.mei_category)
    band = _band_for(consumed, tolerance)

    return ThresholdStatus(
        accumulated=accumulated,
        ceiling=ceiling,
        percentage_consumed=(consumed * 100).quantize(CENTAVO),
        band=band,
        months_counted=months,
        is_opening_year=is_opening_year,
        desenquadramento_effective_on=_effective_date_for(
            band,
            client,
            year,
            is_opening_year=is_opening_year,
        ),
    )

"""Moving a nominal deadline onto a day a payment can actually be settled.

**The direction is read from the rule, never assumed.** Resolução CGSN nº 140/2018
art. 40 §3 postpones the DAS FORWARD to the next business day; art. 109 fixes the
DASN and it does not move at all; DAE-MEI, the employee payroll guide, is anticipated
BACKWARD. Three obligations in the same product, three behaviours, and a resolver
holding an opinion of its own would be right about one of them.

**Runs are cleared, not single days.** 20 November 2026 is Consciência Negra and a
Friday, so the DAS for competence October 2026 must clear three consecutive
unavailable days. An implementation that steps once returns Saturday the 21st — a
real date and an impossible deadline, with nothing to indicate it is wrong.

This module is named `calendar` inside `apps.obligations`, which does NOT shadow the
standard library: Python 3 resolves `import calendar` absolutely, so `rules.py` still
gets the stdlib module.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, timedelta

from apps.obligations.holidays import HolidayScope, is_business_day
from apps.obligations.models import ObligationType
from apps.obligations.rules import Direction, DueRule, nominal_due_date, parse_due_rule

# Longer than any real run of non-business days: the record in Brazil is Carnaval
# adjoining a weekend, four days. Reaching this bound means the holiday table is
# wrong, not that the calendar is unusual.
MAX_BUSINESS_DAY_SHIFT = 15


@dataclass(frozen=True)
class ResolvedDueDate:
    """A deadline before and after business-day adjustment.

    Both are kept because both are stored. The nominal date is what the rule says
    and is stable; the resolved date is what the client must actually pay by and
    changes if the holiday table is corrected. Keeping only the resolved one would
    make a later calendar fix indistinguishable from a rule change.
    """

    nominal: date
    resolved: date


def resolve_due_date(
    nominal_date: date,
    rule: DueRule,
    scopes: Collection[str] = (HolidayScope.NATIONAL,),
) -> date:
    """Move `nominal_date` in the rule's direction until it is a business day."""
    if rule.direction is Direction.NONE:
        return nominal_date

    step = timedelta(days=1 if rule.direction is Direction.FORWARD else -1)
    day = nominal_date
    for _attempt in range(MAX_BUSINESS_DAY_SHIFT):
        if is_business_day(day, scopes):
            return day
        day += step

    msg = (
        f"No business day found within {MAX_BUSINESS_DAY_SHIFT} days "
        f"{rule.direction.value} of {nominal_date.isoformat()}. The holiday table is "
        f"almost certainly wrong; refusing to keep walking, because this runs in a "
        f"task with acks_late where an unbounded loop is retried forever in silence."
    )
    raise ValueError(msg)


def due_date_for(
    obligation_type: ObligationType,
    competence: date,
    scopes: Collection[str] = (HolidayScope.NATIONAL,),
) -> ResolvedDueDate:
    """Place and then adjust the deadline for one competence period.

    Everything that decides the answer — the day, the month, the direction, the
    periodicity — comes off the `ObligationType` row. Editing that row changes the
    deadline with no deploy, which is the property the obligation engine exists for.
    """
    rule = parse_due_rule(obligation_type.due_rule)
    nominal = nominal_due_date(rule, competence, obligation_type.periodicity)
    return ResolvedDueDate(
        nominal=nominal,
        resolved=resolve_due_date(nominal, rule, scopes),
    )

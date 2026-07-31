"""Reading a `due_rule` row, and placing the deadline it describes.

This module answers "which calendar day does this obligation nominally fall on?".
Whether that day then moves — and in which direction — is `calendar.resolve_due_date`,
because moving it requires a holiday table and placing it does not.

**Direction is data, and that is not over-engineering.** The two rules shipped here
already disagree: DAS-MEI is postponed FORWARD to the next business day (Resolução
CGSN nº 140/2018 art. 40 §3) and DASN-SIMEI does not move at all (art. 109). DAE-MEI,
the employee payroll guide, is anticipated BACKWARD. Three obligations, three
behaviours, one column.

**A deadline reports on a period that has already closed**, so it falls in the period
after the one it covers: a monthly obligation is due in the following month, an annual
one in the following year. That single rule places both DAS and DASN, which is why
neither needs an offset field and why the seeded DASN rule is exactly the three keys
the plan specifies.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from django.db import models

from apps.obligations.models import ObligationDueRule, ObligationType, Periodicity

MONTHS_IN_YEAR = 12


class Direction(StrEnum):
    """Which way a deadline moves when it lands on a day that is not a business day.

    `NONE` is a first-class member, not a null. DASN-SIMEI genuinely does not move,
    and modelling that as "no rule" would make the absence of a rule mean two things:
    "does not roll" and "nobody has decided yet".
    """

    FORWARD = "forward"
    BACKWARD = "backward"
    NONE = "none"


@dataclass(frozen=True)
class DueRule:
    """A parsed `ObligationType.due_rule`.

    `month` is None for a monthly obligation, which takes the month from the
    competence it reports on; an annual obligation names its month outright.
    """

    day: int
    direction: Direction
    month: int | None = None


def parse_due_rule(raw: dict[str, object]) -> DueRule:
    """Parse a stored rule, refusing anything this engine cannot honour.

    Refusing rather than defaulting is the point. A rule with an unrecognised
    direction that quietly became `forward` would apply the DAS regime to an
    obligation whose real regime anticipates backward, and the resulting date would
    be a perfectly plausible business day.
    """
    raw_direction = raw.get("direction")
    try:
        direction = Direction(str(raw_direction))
    except ValueError as exc:
        permitted = ", ".join(sorted(member.value for member in Direction))
        msg = (
            f"due_rule direction {raw_direction!r} is not one of {permitted}. "
            f"Refusing to default: an unrecognised direction silently treated as "
            f"'forward' would move a deadline that the law does not move."
        )
        raise ValueError(msg) from exc

    raw_day = raw.get("day")
    if not isinstance(raw_day, int):
        msg = f"due_rule day must be an integer, got {raw_day!r}"
        raise TypeError(msg)

    raw_month = raw.get("month")
    if raw_month is not None and not isinstance(raw_month, int):
        msg = f"due_rule month must be an integer or absent, got {raw_month!r}"
        raise TypeError(msg)

    return DueRule(day=raw_day, direction=direction, month=raw_month)


# Named without an Error suffix to match `NoEffectiveParameter`, which it is the
# deadline-side twin of, and because it reads correctly at the call site.
class NoEffectiveDueRule(LookupError):  # noqa: N818
    """Raised when no era covers the obligation and date asked about.

    Refusing rather than falling back to the newest era regardless of date is the
    whole point of storing eras. A 2024 competence resolved against a 2027 regime
    would place a deadline that is a perfectly ordinary business day and wrong by
    however much the resolution moved it -- and nothing downstream could tell.
    """


def due_rule_for(obligation_type: ObligationType, on_date: date) -> DueRule:
    """Return the deadline rule in force for `obligation_type` on `on_date`.

    Latest-wins among the covering eras, exactly as `effective_parameter` resolves a
    fiscal parameter: superseding a rule is a pure INSERT, so a resolution published
    on the day it takes effect needs no deploy and no close-then-insert dance.
    """
    row = (
        ObligationDueRule.objects.filter(
            obligation_type=obligation_type,
            valid_from__lte=on_date,
        )
        .filter(models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=on_date))
        .order_by("-valid_from")
        .first()
    )
    if row is None:
        msg = (
            f"No due rule for {obligation_type.pk!r} is in force on "
            f"{on_date.isoformat()}. Seed an era rather than assuming the current "
            f"one: a deadline placed under the wrong regime is a plausible date "
            f"that nothing downstream can question."
        )
        raise NoEffectiveDueRule(msg)
    return parse_due_rule(row.rule)


def _following_period(competence: date, periodicity: str) -> tuple[int, int]:
    if periodicity == Periodicity.ANNUAL:
        return (competence.year + 1, competence.month)
    month = competence.month + 1
    if month > MONTHS_IN_YEAR:
        return (competence.year + 1, 1)
    return (competence.year, month)


def nominal_due_date(rule: DueRule, competence: date, periodicity: str) -> date:
    """Place the deadline for `competence`, before any business-day adjustment.

    Raises rather than clamping when the rule names a day the target month does not
    have. Silently landing a "day 31" rule on the 28th of February would move a legal
    deadline by three days and produce a date that looks entirely ordinary.
    """
    year, month = _following_period(competence, periodicity)
    if rule.month is not None:
        month = rule.month
    days_in_month = monthrange(year, month)[1]
    if rule.day > days_in_month:
        msg = (
            f"due_rule asks for day {rule.day} of {year}-{month:02d}, which has only "
            f"{days_in_month} days. Refusing to clamp: quietly moving a statutory "
            f"deadline produces a plausible date and no error."
        )
        raise ValueError(msg)
    return date(year, month, rule.day)

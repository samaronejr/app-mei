"""Where a deadline falls, computed from the seeded rule and from nothing else.

The two obligations this product ships disagree about rolling, and they disagree in a
way no amount of care in a single code path would survive: DAS-MEI is postponed
forward to the next business day (Resolução CGSN nº 140/2018 art. 40 §3) while
DASN-SIMEI does not move at all, even when 31 May falls on a Saturday or a Sunday
(art. 109). A third, DAE-MEI, anticipates backward. Direction is therefore data.

The anchor document's "last business day of May" for DASN is wrong, and 2025 settled
it in public: 31 May 2025 was a Saturday and the deadline did not move.
"""

from datetime import date
from pathlib import Path

import pytest

from apps.obligations.models import ObligationDueRule, ObligationType, Periodicity
from apps.obligations.rules import Direction, nominal_due_date, parse_due_rule

pytestmark = pytest.mark.django_db

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUNDAY = 6


def dasn() -> ObligationType:
    """Return the seeded DASN-SIMEI obligation type."""
    return ObligationType.objects.get(pk="DASN")


def das() -> ObligationType:
    """Return the seeded DAS-MEI obligation type."""
    return ObligationType.objects.get(pk="DAS")


def current_era(code: str) -> ObligationDueRule:
    """Return the open-ended era for `code` — the regime in force today.

    Selected by `valid_to IS NULL` rather than by taking the only row, so the day a
    successor era is seeded this helper keeps naming the current regime instead of
    raising `MultipleObjectsReturned` and reading as an unrelated breakage.
    """
    return ObligationDueRule.objects.get(
        obligation_type_id=code,
        valid_to__isnull=True,
    )


def test_the_dasn_rule_is_seeded_exactly_as_specified() -> None:
    # Given the era seeded by migration
    era = current_era("DASN")

    # When its rule is read
    # Then it is 31 May with no rolling, and the periodicity is annual
    assert era.rule == {"month": 5, "day": 31, "direction": "none"}
    assert dasn().periodicity == Periodicity.ANNUAL


def test_none_is_a_first_class_direction_not_a_special_case() -> None:
    # Given the seeded DASN rule
    rule = parse_due_rule(current_era("DASN").rule)

    # When its direction is parsed
    # Then `none` is a member of the same enumeration as forward and backward,
    # so the resolver dispatches on it rather than branching on the obligation code
    assert rule.direction is Direction.NONE
    assert Direction.NONE in set(Direction)


def test_dasn_for_competence_2026_falls_on_31_may_2027() -> None:
    # Given the seeded DASN rule
    rule = parse_due_rule(current_era("DASN").rule)

    # When the deadline for calendar year 2026 is placed
    due = nominal_due_date(rule, date(2026, 1, 1), Periodicity.ANNUAL)

    # Then it lands on 31 May of the FOLLOWING year. An annual declaration reports
    # on a year that has to finish before it can be declared.
    assert due == date(2027, 5, 31)


def test_dasn_stays_on_31_may_in_a_year_when_that_is_a_sunday() -> None:
    # Given the seeded DASN rule and competence year 2025, whose deadline falls in
    # 2026 — and 31 May 2026 is a SUNDAY
    rule = parse_due_rule(current_era("DASN").rule)
    assert date(2026, 5, 31).weekday() == SUNDAY, "this case must land on a weekend"

    # When the deadline is placed
    due = nominal_due_date(rule, date(2025, 1, 1), Periodicity.ANNUAL)

    # Then it is still 31 May. The rule's direction is `none`, so this nominal date
    # IS the resolved date — there is no calendar to consult and nothing to shift.
    # T-040's resolver is separately proven to preserve it end to end.
    assert due == date(2026, 5, 31)
    assert rule.direction is Direction.NONE


def test_dasn_for_competence_2024_falls_on_a_saturday_and_does_not_move() -> None:
    # Given competence 2024, whose deadline fell on Saturday 31 May 2025 — the year
    # that settled this question in public
    rule = parse_due_rule(current_era("DASN").rule)

    # When the deadline is placed
    due = nominal_due_date(rule, date(2024, 1, 1), Periodicity.ANNUAL)

    # Then it is 31 May 2025, a Saturday, exactly as RFB applied it
    assert due == date(2025, 5, 31)
    assert due.weekday() == SUNDAY - 1


def test_the_dasn_row_cites_the_resolution_it_comes_from() -> None:
    # Given the seeded row
    row = dasn()

    # When its provenance is read
    # Then it names the article, so the next person who reads "no rolling" and
    # doubts it has somewhere to look instead of re-researching it
    assert row.source_url
    assert "140/2018" in row.source_note
    assert "109" in row.source_note


def test_das_is_seeded_as_day_20_rolling_forward() -> None:
    # Given the seeded DAS rule
    row = das()
    rule = parse_due_rule(current_era("DAS").rule)

    # When it is read
    # Then it is day 20, monthly, rolling FORWARD. The anchor was right about this
    # one and earlier research was wrong; art. 40 §3 says "dia útil imediatamente
    # posterior".
    assert row.periodicity == Periodicity.MONTHLY
    assert rule.day == 20
    assert rule.direction is Direction.FORWARD
    assert rule.month is None


def test_das_falls_on_day_20_of_the_month_after_the_competence() -> None:
    # Given the seeded DAS rule
    rule = parse_due_rule(current_era("DAS").rule)

    # When deadlines are placed for two competence months
    # Then each lands on the 20th of the following month, December rolling the year
    assert nominal_due_date(rule, date(2026, 6, 1), Periodicity.MONTHLY) == date(
        2026,
        7,
        20,
    )
    assert nominal_due_date(rule, date(2026, 12, 1), Periodicity.MONTHLY) == date(
        2027,
        1,
        20,
    )


def test_the_das_row_cites_the_paragraph_that_sets_the_direction() -> None:
    # Given the seeded row
    row = das()

    # When its provenance is read
    # Then it names art. 40 §3, the paragraph that says the payment is postponed
    assert row.source_url
    assert "140/2018" in row.source_note
    assert "40" in row.source_note


def test_a_rule_naming_a_day_its_month_does_not_have_is_refused() -> None:
    # Given a monthly rule asking for day 31
    rule = parse_due_rule({"day": 31, "direction": "forward"})

    # When it is applied to a competence whose following month is February
    # Then it raises rather than silently clamping. Quietly moving a deadline to the
    # 28th is the exact class of error this whole wave exists to prevent, and it
    # would be invisible: the resulting date is a real, plausible date.
    with pytest.raises(ValueError, match="day 31"):
        nominal_due_date(rule, date(2026, 1, 1), Periodicity.MONTHLY)


def test_an_unknown_direction_is_refused_rather_than_defaulted() -> None:
    # Given a rule carrying a direction nobody implemented
    # When it is parsed
    # Then it raises. Defaulting to forward would silently apply the DAS rule to an
    # obligation whose real regime anticipates backward.
    with pytest.raises(ValueError, match="sideways"):
        parse_due_rule({"day": 20, "direction": "sideways"})


def test_no_code_path_special_cases_dasn_by_code() -> None:
    # Given every application source outside migrations, which legitimately seed the
    # row and must therefore name it
    sources = [
        path
        for path in (PROJECT_ROOT / "apps").rglob("*.py")
        if "migrations" not in path.parts and "__pycache__" not in path.parts
    ]
    assert len(sources) > 10, "the scan found nothing and would pass vacuously"

    # When they are searched for the QUOTED literal — which is the shape a branch
    # takes: `if code == "DASN"`, `{"DASN": ...}`, `code="DASN"`. Prose mentioning
    # DASN in a docstring is not a branch and is deliberately not matched.
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}:{number + 1}: {line.strip()}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if '"DASN"' in line or "'DASN'" in line
    ]

    # Then there are none. The deadline comes from the seeded due_rule; an
    # `if code == "DASN"` branch would mean the next obligation adds another, and
    # the "engine is data" property would be gone one commit at a time.
    assert offenders == [], (
        "DASN is special-cased in behavioural code rather than driven by its "
        "due_rule row:\n  " + "\n  ".join(offenders)
    )

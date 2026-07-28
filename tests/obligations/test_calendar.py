"""Business-day rolling, in the direction the DATA says, over runs of any length.

The direction is the whole point. DAS-MEI is postponed FORWARD (Resolução CGSN nº
140/2018 art. 40 §3), DASN-SIMEI does not move (art. 109), and DAE-MEI — the employee
payroll guide, a different regime entirely — is anticipated BACKWARD. Three
behaviours. An engine that hardcoded "roll forward" would be right about the DAS and
would place the other two on days the law does not recognise.

Chains are not an edge case here, they are the common case. 20 November 2026 is
Consciência Negra and a Friday, so the DAS for competence October 2026 has to clear a
three-day block, not one day.
"""

from datetime import date

import pytest

from apps.obligations.calendar import due_date_for, resolve_due_date
from apps.obligations.models import ObligationType
from apps.obligations.rules import Direction, DueRule, parse_due_rule

pytestmark = pytest.mark.django_db

FORWARD = DueRule(day=20, direction=Direction.FORWARD)
BACKWARD = DueRule(day=20, direction=Direction.BACKWARD)
NEVER = DueRule(day=31, direction=Direction.NONE, month=5)


def test_an_ordinary_business_day_is_returned_unchanged() -> None:
    # Given Monday 22 June 2026, which is neither a weekend nor a holiday
    # When it is resolved forward
    # Then it does not move. Without this control every assertion below would pass
    # on an implementation that always advanced by one day.
    assert resolve_due_date(date(2026, 6, 22), FORWARD) == date(2026, 6, 22)


def test_a_saturday_rolls_forward_to_the_following_monday() -> None:
    # Given Saturday 20 June 2026 — the DAS nominal date for competence May 2026
    # When it is resolved forward
    # Then it lands on Monday the 22nd, skipping the whole weekend rather than
    # advancing one day onto a Sunday
    assert resolve_due_date(date(2026, 6, 20), FORWARD) == date(2026, 6, 22)


def test_a_sunday_rolls_forward_to_the_next_day() -> None:
    # Given Sunday 20 September 2026
    # When it is resolved forward
    # Then Monday the 21st
    assert resolve_due_date(date(2026, 9, 20), FORWARD) == date(2026, 9, 21)


def test_a_midweek_holiday_rolls_forward_past_the_holiday_alone() -> None:
    # Given Tiradentes, Tuesday 21 April 2026, with ordinary weekdays around it
    # When it is resolved forward
    # Then Wednesday the 22nd — one day, because there is nothing else to clear
    assert resolve_due_date(date(2026, 4, 21), FORWARD) == date(2026, 4, 22)


def test_a_holiday_adjacent_to_a_weekend_skips_the_entire_run() -> None:
    # Given Friday 20 November 2026, which is Consciência Negra, so Friday, Saturday
    # and Sunday are all unavailable
    # When it is resolved forward
    # Then Monday the 23rd. An implementation that advanced a single day would
    # return Saturday the 21st, which is a real date and an impossible deadline.
    assert resolve_due_date(date(2026, 11, 20), FORWARD) == date(2026, 11, 23)


def test_the_same_run_is_cleared_in_the_other_direction_too() -> None:
    # Given Sunday 22 November 2026, with Saturday and the Friday holiday behind it
    # When it is resolved backward
    # Then Thursday the 19th — three days cleared going the other way
    assert resolve_due_date(date(2026, 11, 22), BACKWARD) == date(2026, 11, 19)


def test_a_saturday_anticipates_backward_to_the_previous_friday() -> None:
    # Given Saturday 20 June 2026 and a BACKWARD rule, which is the DAE-MEI regime
    # When it is resolved
    # Then Friday the 19th. Same input, opposite answer from the forward case —
    # which is exactly why direction cannot be a constant in the resolver.
    assert resolve_due_date(date(2026, 6, 20), BACKWARD) == date(2026, 6, 19)
    assert resolve_due_date(date(2026, 6, 20), FORWARD) == date(2026, 6, 22)


def test_direction_none_returns_a_sunday_unchanged() -> None:
    # Given Sunday 31 May 2026 and a rule that does not roll
    # When it is resolved
    # Then it is returned untouched. This is the DASN case, and it is served by a
    # first-class Direction member rather than by a branch on the obligation code.
    assert resolve_due_date(date(2026, 5, 31), NEVER) == date(2026, 5, 31)


def test_direction_none_returns_a_saturday_unchanged() -> None:
    # Given Saturday 31 May 2025, the year that settled this in public
    # When it is resolved
    # Then it does not move
    assert resolve_due_date(date(2025, 5, 31), NEVER) == date(2025, 5, 31)


def test_dasn_resolves_end_to_end_for_two_years_one_of_them_a_weekend() -> None:
    # Given the seeded DASN obligation type
    dasn = ObligationType.objects.get(pk="DASN")

    # When the deadline is resolved for two competence years, straight from the row
    weekend_year = due_date_for(dasn, date(2025, 1, 1))
    weekday_year = due_date_for(dasn, date(2026, 1, 1))

    # Then the Sunday one holds at 31 May and the ordinary one is unremarkable.
    # The nominal and resolved dates are equal in both, because the rule says so.
    assert weekend_year.nominal == date(2026, 5, 31)
    assert weekend_year.resolved == date(2026, 5, 31)
    assert weekend_year.resolved.weekday() == 6
    assert weekday_year.nominal == date(2027, 5, 31)
    assert weekday_year.resolved == date(2027, 5, 31)


def test_das_resolves_end_to_end_across_a_full_year() -> None:
    # Given the seeded DAS obligation type
    das = ObligationType.objects.get(pk="DAS")

    # When every 2026 competence month is resolved
    resolved = {
        month: due_date_for(das, date(2026, month, 1)).resolved
        for month in range(1, 13)
    }

    # Then the three that fall on a weekend or holiday have moved forward and the
    # rest sit on the 20th
    assert resolved[5] == date(2026, 6, 22)
    assert resolved[8] == date(2026, 9, 21)
    assert resolved[10] == date(2026, 11, 23)
    assert resolved[11] == date(2026, 12, 21)
    assert resolved[6] == date(2026, 7, 20)
    assert all(day.weekday() < 5 for day in resolved.values())


def test_changing_the_direction_in_data_changes_the_answer_with_no_code_edit() -> None:
    # Given the seeded DAS rule, whose nominal date for competence May 2026 is a
    # Saturday, and the date it currently resolves to
    das = ObligationType.objects.get(pk="DAS")
    competence = date(2026, 5, 1)
    before = due_date_for(das, competence).resolved
    assert before == date(2026, 6, 22)

    # When ONLY the stored rule is edited — no deploy, no code change
    das.due_rule = {**das.due_rule, "direction": "backward"}
    das.save(update_fields=["due_rule"])

    # Then the resolved date moves the other way. This is the property the whole
    # "engine is data" design exists to provide, asserted rather than asserted-about.
    after = due_date_for(ObligationType.objects.get(pk="DAS"), competence).resolved
    assert after == date(2026, 6, 19)
    assert after != before


def test_turning_the_rolling_off_in_data_leaves_the_nominal_date() -> None:
    # Given the seeded DAS rule and a competence whose nominal date is a Saturday
    das = ObligationType.objects.get(pk="DAS")
    competence = date(2026, 5, 1)

    # When the direction is switched to none in data alone
    das.due_rule = {**das.due_rule, "direction": "none"}
    das.save(update_fields=["due_rule"])

    # Then the deadline stays on the Saturday, proving `none` is dispatched from the
    # same enumeration as the other two rather than handled by a special case
    result = due_date_for(ObligationType.objects.get(pk="DAS"), competence)
    assert result.resolved == date(2026, 6, 20)
    assert result.resolved == result.nominal


def test_an_endless_run_of_holidays_raises_instead_of_looping_forever() -> None:
    # Given a calendar in which every day near the nominal date is a holiday, which
    # is what a data-entry error in a bulk holiday import looks like
    from apps.obligations.holidays import HolidayScope  # noqa: PLC0415
    from apps.obligations.models import Holiday  # noqa: PLC0415

    start = date(2026, 6, 1)
    Holiday.objects.bulk_create(
        Holiday(
            date=date.fromordinal(start.toordinal() + offset),
            name=f"bogus-{offset}",
            scope=HolidayScope.NATIONAL,
        )
        for offset in range(40)
    )

    # When a date inside the run is resolved
    # Then it raises rather than walking the calendar indefinitely. This runs inside
    # a Celery task with acks_late, so an unbounded loop is not a hang — it is a
    # worker that is retried forever and never reports anything.
    with pytest.raises(ValueError, match="business day"):
        resolve_due_date(date(2026, 6, 10), FORWARD)


def test_the_resolver_reads_the_rule_it_is_given_not_a_global_default() -> None:
    # Given the seeded DASN rule parsed from the database
    rule = parse_due_rule(ObligationType.objects.get(pk="DASN").due_rule)

    # When a Sunday is resolved with it
    # Then it is unchanged, because that rule says so — the resolver holds no
    # opinion of its own about which obligations roll
    assert resolve_due_date(date(2026, 5, 31), rule) == date(2026, 5, 31)

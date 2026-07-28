"""The national holiday calendar, and the Easter computation the movable feasts ride on.

Three of the dates that shift a DAS deadline every year — Carnaval, Sexta-feira Santa
and Corpus Christi — are defined as offsets from Easter, which itself moves across a
35-day window on a 5,700,000-year cycle. Hardcoding them is a maintenance bomb with a
one-year fuse: the table is right until the January nobody remembers to refill it, and
then every DAS due date in the year is silently one to three days early.

So the computation is tested against known Easter dates spanning both centuries and
both branches of the Gregorian correction, and the seed materializes what it produces.
"""

from datetime import date

import pytest

from apps.obligations.holidays import (
    HolidayScope,
    easter_sunday,
    is_business_day,
    national_holidays,
)
from apps.obligations.models import Holiday

pytestmark = pytest.mark.django_db

# Published Easter Sundays. Spread deliberately across centuries so a century-rule
# error in the Gregorian correction cannot pass by matching a single decade.
KNOWN_EASTER = {
    1961: date(1961, 4, 2),
    1981: date(1981, 4, 19),
    2000: date(2000, 4, 23),
    2008: date(2008, 3, 23),
    2024: date(2024, 3, 31),
    2025: date(2025, 4, 20),
    2026: date(2026, 4, 5),
    2027: date(2027, 3, 28),
    2028: date(2028, 4, 16),
    2038: date(2038, 4, 25),
    2100: date(2100, 3, 28),
}

HOLIDAYS_PER_YEAR = 13
SUNDAY = 6

# 2026 and 2027, computed independently of the implementation and checked against
# published calendars. The movable ones are the point of this table.
EXPECTED_2026 = {
    date(2026, 1, 1): "Confraternização Universal",
    date(2026, 2, 16): "Carnaval",
    date(2026, 2, 17): "Carnaval",
    date(2026, 4, 3): "Sexta-feira Santa",
    date(2026, 4, 21): "Tiradentes",
    date(2026, 5, 1): "Dia do Trabalho",
    date(2026, 6, 4): "Corpus Christi",
    date(2026, 9, 7): "Independência do Brasil",
    date(2026, 10, 12): "Nossa Senhora Aparecida",
    date(2026, 11, 2): "Finados",
    date(2026, 11, 15): "Proclamação da República",
    date(2026, 11, 20): "Consciência Negra",
    date(2026, 12, 25): "Natal",
}
EXPECTED_2027_MOVABLE = {
    date(2027, 2, 8): "Carnaval",
    date(2027, 2, 9): "Carnaval",
    date(2027, 3, 26): "Sexta-feira Santa",
    date(2027, 5, 27): "Corpus Christi",
}


@pytest.mark.parametrize(("year", "expected"), sorted(KNOWN_EASTER.items()))
def test_easter_is_computed_not_looked_up(year: int, expected: date) -> None:
    # Given a year with a published Easter Sunday
    # When it is computed
    computed = easter_sunday(year)

    # Then it matches, and it is a Sunday. The second assertion catches an
    # off-by-one that would otherwise agree with the table for a lucky year.
    assert computed == expected
    assert computed.weekday() == SUNDAY


def test_every_2026_national_holiday_is_seeded_on_the_right_day() -> None:
    # Given the seed applied by migration
    rows = {
        row.date: row.name
        for row in Holiday.objects.filter(date__year=2026, scope=HolidayScope.NATIONAL)
    }

    # When the 2026 calendar is compared with the published one
    # Then every date matches, including the four derived from Easter
    assert set(rows) == set(EXPECTED_2026)
    for day, name in EXPECTED_2026.items():
        assert name in rows[day], f"{day} is seeded as {rows[day]!r}, expected {name}"


def test_the_2027_movable_feasts_are_seeded_on_their_own_dates() -> None:
    # Given the seed
    rows = {
        row.date
        for row in Holiday.objects.filter(date__year=2027, scope=HolidayScope.NATIONAL)
    }

    # When the movable feasts are checked
    # Then 2027's differ from 2026's, which is the whole reason they are computed.
    # A hardcoded table copied forward would repeat February 16 and fail here.
    assert set(EXPECTED_2027_MOVABLE) <= rows
    assert date(2026, 2, 16) not in rows


def test_both_seeded_years_are_complete() -> None:
    # Given the seed
    # When each year is counted
    # Then both hold the full national calendar, so a partially applied seed is a
    # failing test rather than a quietly short year
    for year in (2026, 2027):
        count = Holiday.objects.filter(
            date__year=year,
            scope=HolidayScope.NATIONAL,
        ).count()
        assert count == HOLIDAYS_PER_YEAR, f"{year} has {count} holidays"


def test_the_computed_calendar_matches_the_seeded_one() -> None:
    # Given the computation and the seeded rows for 2026
    computed = {day for day, _name in national_holidays(2026)}
    seeded = {
        row.date
        for row in Holiday.objects.filter(date__year=2026, scope=HolidayScope.NATIONAL)
    }

    # When they are compared
    # Then they agree. The migration materializes the computation rather than
    # carrying a second, independently maintained list that can drift from it.
    assert computed == seeded


def test_a_weekend_is_not_a_business_day() -> None:
    # Given a Saturday and the Sunday after it, neither of them a holiday
    saturday = date(2026, 6, 20)
    sunday = date(2026, 6, 21)
    assert saturday.weekday() == SUNDAY - 1

    # When each is classified
    # Then neither is a business day
    assert is_business_day(saturday) is False
    assert is_business_day(sunday) is False


def test_an_ordinary_weekday_is_a_business_day() -> None:
    # Given a Monday that is not a holiday
    # When it is classified
    # Then it is a business day. Without this control every assertion below would
    # pass on an implementation that always answered False.
    assert is_business_day(date(2026, 6, 22)) is True


def test_a_midweek_holiday_is_not_a_business_day() -> None:
    # Given Tiradentes 2026, which falls on a Tuesday
    tiradentes = date(2026, 4, 21)
    assert tiradentes.weekday() < SUNDAY - 1, "this case must land midweek"

    # When it is classified
    # Then it is not a business day, purely because the table says so — the date is
    # an ordinary Tuesday in every other respect
    assert is_business_day(tiradentes) is False


def test_a_movable_feast_is_not_a_business_day_either() -> None:
    # Given Corpus Christi 2026, a Thursday computed as Easter + 60
    corpus = date(2026, 6, 4)
    assert corpus.weekday() < SUNDAY - 1

    # When it is classified
    # Then it is not a business day. This is the case a fixed-date table gets wrong
    # in every year it was not written for.
    assert is_business_day(corpus) is False


def test_a_municipal_holiday_does_not_make_a_national_business_day_disappear() -> None:
    # Given a municipal holiday recorded on an ordinary national business day
    workday = date(2026, 6, 22)
    Holiday.objects.create(
        date=workday,
        name="Aniversário da cidade",
        scope=HolidayScope.MUNICIPAL,
    )

    # When the day is classified with the default national calendar
    # Then it is still a business day. The scope column exists now so municipal
    # holidays need no migration later, and a lookup ignoring scope would let one
    # city's holiday move every client's DAS in the country.
    assert is_business_day(workday) is True
    assert is_business_day(workday, scopes=(HolidayScope.MUNICIPAL,)) is False


def test_seeding_twice_does_not_duplicate_a_holiday() -> None:
    # Given the already-seeded calendar
    before = Holiday.objects.count()

    # When the seed runs again, as it does on every migrate and after a
    # transactional test truncates the table
    import importlib  # noqa: PLC0415

    module = importlib.import_module(
        "apps.obligations.migrations.0005_seed_national_holidays",
    )
    module.seed_from_global_registry()

    # Then the count is unchanged
    assert Holiday.objects.count() == before

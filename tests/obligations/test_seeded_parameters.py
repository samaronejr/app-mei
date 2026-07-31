"""The six 2026 parameters, their categories, and the rows deliberately NOT seeded.

Six, not seven: `das.due_day` was retired by `0015_seed_due_rule_eras`, which moved the
DAS due day into the era table where the rest of that rule already lives. The count
assertion below is what proves the retirement reaches BOTH seed paths — the migration
and the suite's own replay after a transactional test truncates the table.

Every assertion here is about a number an accountant will act on. The category split
matters twice over: the annual ceiling and the proportional monthly rate are BOTH
per-category, so seeding only the ceiling would still measure a caminhoneiro's opening
year against R$ 6.750/month and report a compliant client as over the limit.

The absence assertions matter as much as the presence ones. The 2027 and 2028 ceiling
increases are proposed and not enacted, and a product that seeds unenacted law tells
every firm on it that a client is compliant when Receita Federal says otherwise.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from apps.fiscal.mei import MEICategory
from apps.obligations.models import FiscalParameter
from apps.obligations.parameters import effective_parameter, parameter_for

pytestmark = pytest.mark.django_db

SEEDED_ON = date(2026, 7, 27)
EXPECTED_ROW_COUNT = 6

# key, category, value — the table the plan specifies, restated so a drifted seed is
# a failing test rather than a silent change to a number nobody re-reads.
EXPECTED = (
    ("mei.annual_ceiling", MEICategory.COMMON, Decimal("81000.00")),
    ("mei.annual_ceiling", MEICategory.CAMINHONEIRO, Decimal("251600.00")),
    ("mei.monthly_proportional", MEICategory.COMMON, Decimal("6750.00")),
    ("mei.monthly_proportional", MEICategory.CAMINHONEIRO, Decimal("20966.67")),
    ("mei.excess_tolerance_pct", None, Decimal("0.20")),
    ("mei.max_employees", None, Decimal(1)),
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCH_DOC = PROJECT_ROOT / "docs" / "regulatory-watch.md"


@pytest.mark.parametrize(("key", "category", "value"), EXPECTED)
def test_every_seeded_parameter_resolves_for_a_2026_date(
    key: str,
    category: str | None,
    value: Decimal,
) -> None:
    # Given the seed applied by migration
    # When each parameter is resolved for a mid-2026 date
    # Then it answers with the documented value for its own category
    assert parameter_for(key, SEEDED_ON, category) == value


def test_the_two_annual_ceilings_are_category_aware() -> None:
    # Given both ceilings seeded
    # When each category asks
    common = parameter_for("mei.annual_ceiling", SEEDED_ON, MEICategory.COMMON)
    trucker = parameter_for("mei.annual_ceiling", SEEDED_ON, MEICategory.CAMINHONEIRO)

    # Then they differ. A single global ceiling would measure every trucker against
    # R$ 81.000 and desenquadrar them on paper at a third of their real limit.
    assert common == Decimal("81000.00")
    assert trucker == Decimal("251600.00")
    assert trucker > common


def test_the_proportional_monthly_rate_is_category_aware_too() -> None:
    # Given both proportional rates seeded
    # When each category asks
    common = parameter_for("mei.monthly_proportional", SEEDED_ON, MEICategory.COMMON)
    trucker = parameter_for(
        "mei.monthly_proportional",
        SEEDED_ON,
        MEICategory.CAMINHONEIRO,
    )

    # Then they differ as well. Seeding only the ceiling per category is the subtle
    # half-fix: the opening-year branch would still use R$ 6.750 for a trucker.
    assert common == Decimal("6750.00")
    assert trucker == Decimal("20966.67")


def test_each_proportional_rate_is_its_own_ceiling_over_twelve() -> None:
    # Given the ceiling and the proportional rate for each category
    for category in (MEICategory.COMMON, MEICategory.CAMINHONEIRO):
        ceiling = parameter_for("mei.annual_ceiling", SEEDED_ON, category)
        monthly = parameter_for("mei.monthly_proportional", SEEDED_ON, category)

        # When the two are compared
        # Then the monthly rate is the annual limit divided across twelve months,
        # to the centavo. A transposed digit in either row would break this.
        assert monthly == (ceiling / 12).quantize(Decimal("0.01"))


def test_every_seeded_row_carries_a_source_url() -> None:
    # Given every seeded row
    rows = FiscalParameter.objects.all()
    assert rows.count() == EXPECTED_ROW_COUNT

    # When each is inspected
    # Then all cite a source. A limit with no provenance cannot be re-verified when
    # the law changes, and this table exists precisely to be re-verified.
    unsourced = [row.key for row in rows if not row.source_url.strip()]
    assert unsourced == []
    unexplained = [row.key for row in rows if not row.source_note.strip()]
    assert unexplained == []


def test_every_seeded_row_states_its_category_explicitly() -> None:
    # Given the seeded rows
    actual = {(row.key, row.mei_category) for row in FiscalParameter.objects.all()}

    # When their (key, category) pairs are compared with the specification
    # Then each is exactly as documented, NULL included. Leaving a category
    # unstated forces a guess, and the two guesses resolve differently.
    expected = {(key, category or None) for key, category, _value in EXPECTED}
    assert actual == expected


def test_every_seeded_row_is_open_ended_from_the_start_of_2026() -> None:
    # Given the seeded rows
    rows = list(FiscalParameter.objects.all())

    # When their validity windows are read
    # Then all start on 1 January 2026 and none is closed. A closed row would make
    # the resolver raise the moment the window lapsed, mid-year, with no warning.
    assert {row.valid_from for row in rows} == {date(2026, 1, 1)}
    assert {row.valid_to for row in rows} == {None}


def test_no_unenacted_2027_or_2028_ceiling_was_seeded() -> None:
    # Given the seed
    # When rows starting after 2026 are counted
    future = FiscalParameter.objects.filter(valid_from__gt=date(2026, 12, 31))

    # Then there are none. PLP 186/2026 (R$ 110.000 in 2027, R$ 140.000 in 2028) and
    # PLP 108/2021 (R$ 130.000) are both PENDING. Seeding proposed law would tell a
    # firm its client is compliant while Receita Federal desenquadra them.
    assert list(future) == []


def test_a_2027_date_still_resolves_to_the_enacted_2026_ceiling() -> None:
    # Given only the 2026 rows, which are open-ended
    # When a 2027 date is asked for
    # Then the 2026 value still answers — the law in force until one is enacted
    assert parameter_for("mei.annual_ceiling", date(2027, 1, 1)) == Decimal("81000.00")
    assert parameter_for("mei.annual_ceiling", date(2028, 6, 1)) == Decimal("81000.00")


def test_the_caminhoneiro_rows_cite_their_own_statute() -> None:
    # Given the caminhoneiro ceiling
    row = effective_parameter(
        "mei.annual_ceiling",
        SEEDED_ON,
        MEICategory.CAMINHONEIRO,
    )

    # When its note is read
    # Then it names LC 188/2021 rather than carrying an unattributed approximation
    assert "188" in row.source_note


def test_the_tolerance_row_cites_the_resolution_that_sets_it() -> None:
    # Given the 20% excess tolerance
    row = effective_parameter("mei.excess_tolerance_pct", SEEDED_ON)

    # When its note is read
    # Then it names Resolução CGSN nº 140/2018 art. 115, which is the article that
    # makes the difference between desenquadramento in January and next January
    assert "140/2018" in row.source_note
    assert "115" in row.source_note


def test_the_regulatory_watch_document_names_the_pending_bills() -> None:
    # Given the watch document
    assert WATCH_DOC.is_file()
    text = WATCH_DOC.read_text(encoding="utf-8")

    # When it is read
    # Then it names both pending bills and the review ritual. A monthly re-check is
    # the only thing standing between an enacted ceiling and a stale seed.
    assert "PLP 186/2026" in text
    assert "PLP 108/2021" in text
    assert "81.000" in text

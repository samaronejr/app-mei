"""The parameter resolver: latest-wins effective dating, and what it refuses.

Every value the obligation engine reads is a row in a table, and the property this
suite protects is that superseding one is a pure INSERT. An accountant-facing product
whose ceiling lives in a constant needs a deploy on the day the law changes, which is
the day it can least afford one.

Overlapping validity ranges are therefore PERMITTED and resolved latest-wins. An
exclusion constraint would look correct and would reject exactly the insert this
property depends on: the open-ended 2026 row spans `[2026-01-01, ∞)`, so any later
open-ended row necessarily overlaps it.
"""

import re
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.fiscal.mei import MEICategory
from apps.obligations.models import FiscalParameter, ObligationType, Periodicity
from apps.obligations.parameters import (
    NoEffectiveParameter,
    effective_parameter,
    parameter_for,
)

pytestmark = pytest.mark.django_db

CEILING = "test.annual_ceiling"
SOURCE = "https://www.gov.br/empresas-e-negocios/pt-br/empreendedor"


def make(
    *,
    key: str = CEILING,
    value: str,
    valid_from: date,
    valid_to: date | None = None,
    mei_category: str | None = MEICategory.COMMON,
) -> FiscalParameter:
    """Write one parameter row, with every field this table requires."""
    return FiscalParameter.objects.create(
        key=key,
        mei_category=mei_category,
        value=value,
        valid_from=valid_from,
        valid_to=valid_to,
        source_note="test fixture",
        source_url=SOURCE,
    )


def test_a_covering_row_resolves_to_its_value() -> None:
    # Given an open-ended 2026 ceiling
    make(value="81000.00", valid_from=date(2026, 1, 1))

    # When it is asked for mid-2026
    # Then the stored value comes back as a Decimal, never a float
    assert parameter_for(CEILING, date(2026, 6, 1)) == Decimal("81000.00")
    assert isinstance(parameter_for(CEILING, date(2026, 6, 1)), Decimal)


def test_a_later_row_supersedes_with_no_code_change() -> None:
    # Given the open-ended 2026 ceiling already in place
    make(value="81000.00", valid_from=date(2026, 1, 1))

    # When a hypothetical 2027 row is INSERTED over it — no close-then-insert dance,
    # no migration, no deploy
    make(value="110000.00", valid_from=date(2027, 1, 1))

    # Then 2027 answers with the new value...
    assert parameter_for(CEILING, date(2027, 6, 1)) == Decimal("110000.00")
    # ...and 2026 still answers with the old one. A resolver that simply took the
    # newest row would pass the first assertion and fail this one.
    assert parameter_for(CEILING, date(2026, 6, 1)) == Decimal("81000.00")


def test_the_boundary_date_belongs_to_the_newer_row() -> None:
    # Given two rows meeting on 1 January
    make(value="81000.00", valid_from=date(2026, 1, 1))
    make(value="110000.00", valid_from=date(2027, 1, 1))

    # When the changeover day itself is asked for
    # Then valid_from is inclusive, so the newer row owns it
    assert parameter_for(CEILING, date(2027, 1, 1)) == Decimal("110000.00")
    assert parameter_for(CEILING, date(2026, 12, 31)) == Decimal("81000.00")


def test_a_date_before_every_row_raises_rather_than_defaulting() -> None:
    # Given a row that starts in 2026
    make(value="81000.00", valid_from=date(2026, 1, 1))

    # When an earlier date is asked for
    # Then the resolver refuses. Returning a default here would silently measure a
    # 2025 competence against a 2026 ceiling and report a compliant client as over.
    with pytest.raises(NoEffectiveParameter, match=CEILING):
        parameter_for(CEILING, date(2025, 12, 31))


def test_an_unseeded_key_raises() -> None:
    # Given an empty table
    # When any key is asked for
    # Then the failure is loud
    with pytest.raises(NoEffectiveParameter, match=re.escape("mei.nonexistent")):
        parameter_for("mei.nonexistent", date(2026, 6, 1))


def test_valid_to_is_exclusive_and_closes_the_row() -> None:
    # Given a row explicitly closed at the end of 2026
    make(value="81000.00", valid_from=date(2026, 1, 1), valid_to=date(2027, 1, 1))

    # When the last covered day and the first uncovered day are asked for
    # Then valid_to is the first day NOT covered
    assert parameter_for(CEILING, date(2026, 12, 31)) == Decimal("81000.00")
    with pytest.raises(NoEffectiveParameter):
        parameter_for(CEILING, date(2027, 1, 1))


def test_the_category_specific_row_is_chosen_over_the_common_one() -> None:
    # Given both categories seeded for the same key and date
    make(value="81000.00", valid_from=date(2026, 1, 1))
    make(
        value="251600.00",
        valid_from=date(2026, 1, 1),
        mei_category=MEICategory.CAMINHONEIRO,
    )

    # When each category asks
    # Then each is measured against its own ceiling. One global value would silently
    # measure every trucker against R$ 81.000.
    common = parameter_for(CEILING, date(2026, 6, 1), MEICategory.COMMON)
    trucker = parameter_for(CEILING, date(2026, 6, 1), MEICategory.CAMINHONEIRO)
    assert common == Decimal("81000.00")
    assert trucker == Decimal("251600.00")


def test_omitting_the_category_resolves_the_common_row() -> None:
    # Given both categories seeded
    make(value="81000.00", valid_from=date(2026, 1, 1))
    make(
        value="251600.00",
        valid_from=date(2026, 1, 1),
        mei_category=MEICategory.CAMINHONEIRO,
    )

    # When no category is supplied
    # Then the common row answers, matching ClientCompany's own default. The higher
    # ceiling is never the fallback: guessing upward would report an over-limit
    # client as compliant, which is the expensive direction to be wrong in.
    assert parameter_for(CEILING, date(2026, 6, 1)) == Decimal("81000.00")


def test_a_category_agnostic_row_answers_every_category() -> None:
    # Given a key seeded with NULL category, meaning "applies to all"
    make(key="test.due_day", value="20", valid_from=date(2026, 1, 1), mei_category=None)

    # When each category asks
    # Then the agnostic row answers all of them, rather than raising for the ones
    # that have no row of their own
    for category in (None, MEICategory.COMMON, MEICategory.CAMINHONEIRO):
        assert parameter_for("test.due_day", date(2026, 6, 1), category) == Decimal(20)


def test_a_category_specific_row_beats_an_agnostic_one_on_the_same_key() -> None:
    # Given both an agnostic row and a caminhoneiro override
    make(key="test.x", value="1", valid_from=date(2026, 1, 1), mei_category=None)
    make(
        key="test.x",
        value="9",
        valid_from=date(2026, 1, 1),
        mei_category=MEICategory.CAMINHONEIRO,
    )

    # When the caminhoneiro asks
    # Then the specific row wins. Falling back before checking for an exact match
    # would make every override inert.
    trucker = parameter_for("test.x", date(2026, 6, 1), MEICategory.CAMINHONEIRO)
    assert trucker == Decimal(9)
    assert parameter_for("test.x", date(2026, 6, 1), MEICategory.COMMON) == Decimal(1)


def test_the_resolver_exposes_the_row_so_its_source_can_be_audited() -> None:
    # Given a seeded row
    make(value="81000.00", valid_from=date(2026, 1, 1))

    # When the row itself is requested rather than its value
    row = effective_parameter(CEILING, date(2026, 6, 1))

    # Then the citation travels with the number. A value with no provenance is not
    # auditable, and this table exists to be audited.
    assert row.source_url
    assert row.value == "81000.00"


def test_a_duplicate_key_category_and_start_date_is_rejected() -> None:
    # Given one row
    make(value="81000.00", valid_from=date(2026, 1, 1))

    # When an identical (key, mei_category, valid_from) triple is inserted
    # Then the database refuses. Latest-wins would otherwise be ambiguous: two rows
    # would tie on valid_from and the answer would depend on physical row order.
    with pytest.raises(IntegrityError), transaction.atomic():
        make(value="99999.00", valid_from=date(2026, 1, 1))


def test_a_duplicate_is_rejected_even_when_the_category_is_null() -> None:
    # Given a category-agnostic row
    make(key="test.due_day", value="20", valid_from=date(2026, 1, 1), mei_category=None)

    # When the same triple is inserted again with a NULL category
    # Then it is STILL refused. PostgreSQL treats NULLs as distinct by default, so
    # without nulls_distinct=False a re-run of the seed would silently duplicate the
    # three agnostic rows and make latest-wins a coin flip.
    with pytest.raises(IntegrityError), transaction.atomic():
        make(
            key="test.due_day",
            value="25",
            valid_from=date(2026, 1, 1),
            mei_category=None,
        )


def test_an_overlapping_open_ended_row_is_permitted() -> None:
    # Given the open-ended 2026 row
    make(value="81000.00", valid_from=date(2026, 1, 1))

    # When another open-ended row starting later is inserted, which necessarily
    # OVERLAPS it
    later = make(value="110000.00", valid_from=date(2027, 1, 1))

    # Then the insert succeeds. This assertion is the plan's headline property: an
    # ExclusionConstraint would reject exactly this row and there would be no way to
    # change a ceiling without a deploy.
    assert later.pk is not None
    assert FiscalParameter.objects.filter(key=CEILING).count() == 2


def test_a_validity_range_that_ends_before_it_starts_is_rejected() -> None:
    # Given nothing
    # When a row is written whose valid_to precedes its valid_from
    # Then the database refuses it. Such a row covers no date at all and would be
    # invisible to every query, which reads as "the parameter is missing".
    with pytest.raises(IntegrityError), transaction.atomic():
        make(
            value="81000.00",
            valid_from=date(2026, 6, 1),
            valid_to=date(2026, 1, 1),
        )


def test_obligation_types_carry_their_rule_as_data() -> None:
    # Given an obligation type written with a structured rule
    written = ObligationType.objects.create(
        code="TEST",
        name="Test obligation",
        periodicity=Periodicity.ANNUAL,
        due_rule={"month": 5, "day": 31, "direction": "none"},
        source_note="test fixture",
        source_url=SOURCE,
    )

    # When it is read back
    reloaded = ObligationType.objects.get(pk=written.pk)

    # Then the rule round-trips as structured data, not as code. Changing a deadline
    # must be an UPDATE, never an edit to a function.
    assert reloaded.due_rule == {"month": 5, "day": 31, "direction": "none"}
    assert reloaded.periodicity == Periodicity.ANNUAL

"""Deadline rules as effective-dated data: which era answers, and when none does.

A due rule is not a permanent property of an obligation. Resolução CGSN nº 140/2018
has already moved a due day once, and the day it moves again every historical calendar
regenerated afterwards would silently restate itself under the new regime — producing
dates that are ordinary business days and wrong. One era per row is what makes the
correction an INSERT and keeps the superseded regime readable.

The refusal tests matter as much as the resolution ones. Falling back to the newest
era regardless of date is the tempting shortcut, and it is exactly the failure this
table exists to prevent.
"""

import importlib
from datetime import date

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.obligations.models import (
    FiscalParameter,
    ObligationDueRule,
    ObligationType,
    Periodicity,
)
from apps.obligations.rules import Direction, NoEffectiveDueRule, due_rule_for

pytestmark = pytest.mark.django_db

# Imported by path because a module name beginning with a digit is not an identifier,
# so `from apps.obligations.migrations.0015... import reverse` is a syntax error.
MIGRATION_0002 = importlib.import_module(
    "apps.obligations.migrations.0002_seed_2026_parameters",
)
MIGRATION_0003 = importlib.import_module(
    "apps.obligations.migrations.0003_seed_obligation_types",
)
MIGRATION_0015 = importlib.import_module(
    "apps.obligations.migrations.0015_seed_due_rule_eras",
)

ERA_FLOOR = date(2000, 1, 1)
RETIRED_KEY = "das.due_day"
CGSN_140 = "http://normas.receita.fazenda.gov.br/sijut2consulta/link.action?idAto=92278"


def das() -> ObligationType:
    """Return the seeded DAS-MEI obligation type."""
    return ObligationType.objects.get(pk="DAS")


def test_every_obligation_type_has_exactly_one_seeded_era() -> None:
    # Given the seed applied by migration
    eras = list(ObligationDueRule.objects.all())

    # When they are counted against the obligation types
    # Then there is one era per type and none is closed. A type with no era resolves
    # to a raise in a nightly sweep, far from whoever left it uncovered.
    assert len(eras) == ObligationType.objects.count()
    assert {era.obligation_type_id for era in eras} == set(
        ObligationType.objects.values_list("code", flat=True),
    )
    assert {era.valid_to for era in eras} == {None}


def test_the_seeded_eras_open_far_below_any_competence_this_engine_sees() -> None:
    # Given the seeded eras
    # When their opening dates are read
    # Then all start in 2000. An era opening in 2026 would leave the DASN competence
    # of January 2025 uncovered, because an annual deadline is resolved as-of the
    # competence it reports on and that competence predates the era.
    assert {era.valid_from for era in ObligationDueRule.objects.all()} == {ERA_FLOOR}


def test_each_seeded_era_carries_the_rule_its_obligation_type_was_seeded_with() -> None:
    # Given the rules 0003 recorded for each type, which are the independent source
    expected = {
        code: rule
        for code, _name, _periodicity, rule, _note in MIGRATION_0003.OBLIGATION_TYPES
    }

    # When the era rows are read
    actual = {
        era.obligation_type_id: era.rule for era in ObligationDueRule.objects.all()
    }

    # Then they agree. A transcription slip here would move a statutory deadline and
    # produce a date nothing downstream could question.
    assert actual == expected


def test_every_seeded_era_cites_its_source() -> None:
    # Given the seeded eras
    eras = list(ObligationDueRule.objects.all())

    # When their provenance is read
    # Then each names where the rule comes from. A deadline with no citation cannot
    # be re-verified when the resolution changes, and re-verification is the point.
    assert [era.obligation_type_id for era in eras if not era.source_url.strip()] == []
    assert [era.obligation_type_id for era in eras if not era.source_note.strip()] == []


def test_the_retired_parameter_is_absent_from_the_migrated_table() -> None:
    # Given a fully migrated database
    # When the retired key is looked for
    # Then it is gone. Two spellings of the same statutory number are one edit away
    # from disagreeing, and the surviving spelling is the era's.
    assert not FiscalParameter.objects.filter(key=RETIRED_KEY).exists()


def test_the_later_era_wins_when_two_cover_the_same_date() -> None:
    # Given a second, open-ended era superseding the seeded one from 2026
    ObligationDueRule.objects.create(
        obligation_type=das(),
        rule={"day": 25, "direction": "forward"},
        valid_from=date(2026, 1, 1),
        source_url=CGSN_140,
        source_note="Hypothetical successor regime, for the resolution test.",
    )

    # When a date both eras cover is asked about
    # Then the greater valid_from answers. Overlap is permitted on purpose: closing
    # the predecessor first would make superseding a rule a two-step dance that an
    # emergency resolution cannot afford.
    assert due_rule_for(das(), date(2026, 6, 1)).day == 25

    # And a date only the older era covers still answers with the older rule
    assert due_rule_for(das(), date(2025, 6, 1)).day == 20


def test_valid_to_is_exclusive_so_the_changeover_day_belongs_to_the_successor() -> None:
    # Given the seeded era closed on 1 October 2026 and a successor opening that day
    seeded = ObligationDueRule.objects.get(obligation_type=das())
    seeded.valid_to = date(2026, 10, 1)
    seeded.save(update_fields=["valid_to"])
    ObligationDueRule.objects.create(
        obligation_type=das(),
        rule={"day": 25, "direction": "forward"},
        valid_from=date(2026, 10, 1),
        source_url=CGSN_140,
        source_note="Hypothetical successor regime, for the boundary test.",
    )

    # When the day before the changeover and the changeover day are asked about
    # Then the boundary falls between them with no day belonging to both. Inclusive
    # valid_to would make 1 October ambiguous, and latest-wins would resolve the
    # ambiguity silently in the successor's favour whether or not that was intended.
    assert due_rule_for(das(), date(2026, 9, 30)).day == 20
    assert due_rule_for(das(), date(2026, 10, 1)).day == 25


def test_a_date_below_the_era_floor_refuses_rather_than_defaulting() -> None:
    # Given the seeded eras, all opening in 2000
    # When a date before every era is asked about
    # Then it raises. Falling back to the newest era would place a 1999 deadline
    # under a rule written decades later, and the result would be a plausible date.
    with pytest.raises(NoEffectiveDueRule, match="1999-12-31"):
        due_rule_for(das(), date(1999, 12, 31))


def test_the_refusal_names_the_obligation_and_tells_the_operator_what_to_do() -> None:
    # Given a date no era covers
    # When the resolver refuses
    with pytest.raises(NoEffectiveDueRule) as raised:
        due_rule_for(das(), date(1999, 12, 31))

    # Then the message identifies the obligation and prescribes seeding, rather than
    # leaving an operator to guess which of two obligations went uncovered.
    assert "DAS" in str(raised.value)
    assert "Seed an era" in str(raised.value)


def test_the_resolver_returns_a_parsed_rule_not_the_raw_json() -> None:
    # Given the seeded DAS era
    # When it is resolved
    rule = due_rule_for(das(), date(2026, 6, 1))

    # Then the direction is an enum member, so callers dispatch on it rather than
    # comparing strings — which is what keeps direction data instead of a branch.
    assert rule.direction is Direction.FORWARD
    assert rule.month is None


def test_two_eras_for_one_obligation_cannot_tie_on_the_same_opening_date() -> None:
    # Given the seeded DAS era
    # When a second era claims the same opening date
    # Then the database refuses. A tie would make latest-wins depend on physical row
    # order, which is not a decision anyone made.
    with pytest.raises(IntegrityError), transaction.atomic():
        ObligationDueRule.objects.create(
            obligation_type=das(),
            rule={"day": 25, "direction": "forward"},
            valid_from=ERA_FLOOR,
            source_url=CGSN_140,
            source_note="Duplicate opening date, which must not be storable.",
        )


def test_an_obligation_type_with_eras_cannot_be_deleted() -> None:
    # Given an obligation type that owns an era
    orphaned_type = ObligationType.objects.create(
        code="TEST",
        name="Fixture obligation, for the protection test",
        periodicity=Periodicity.MONTHLY,
        due_rule={"day": 10, "direction": "forward"},
        source_url=CGSN_140,
    )
    ObligationDueRule.objects.create(
        obligation_type=orphaned_type,
        rule={"day": 10, "direction": "forward"},
        valid_from=ERA_FLOOR,
        source_url=CGSN_140,
        source_note="Era whose existence must block the parent's deletion.",
    )

    # When the type is deleted
    # Then it is protected. Cascading would leave the engine resolving to "no rule in
    # force" and refusing in a nightly sweep, far from whoever deleted the type.
    with pytest.raises(ProtectedError), transaction.atomic():
        orphaned_type.delete()


def test_reversing_the_era_migration_restores_the_state_it_found() -> None:
    # Given the migrated state
    assert ObligationDueRule.objects.exists()
    assert not FiscalParameter.objects.filter(key=RETIRED_KEY).exists()

    # When the migration's own reverse runs against the live registry — called
    # directly, because a backward `migrate obligations …` from a later HEAD would
    # first have to reverse operations that are deliberately one-way
    MIGRATION_0015.reverse(django_apps, None)

    # Then the eras are gone and the retired parameter is back exactly as 0002 seeds
    # it, compared against 0002's own registry rather than against 0015's copy
    assert not ObligationDueRule.objects.exists()
    _key, category, value, source_url, source_note = next(
        row for row in MIGRATION_0002.PARAMETERS if row[0] == RETIRED_KEY
    )
    restored = FiscalParameter.objects.get(key=RETIRED_KEY)
    assert restored.mei_category == category
    assert restored.value == value
    assert restored.source_url == source_url
    assert restored.source_note == source_note
    assert restored.valid_from == MIGRATION_0002.EFFECTIVE_FROM
    assert restored.valid_to is None


def test_replaying_the_era_migration_forward_restores_the_migrated_state() -> None:
    # Given a database reversed back to the pre-era state
    MIGRATION_0015.reverse(django_apps, None)

    # When the forward function runs again
    MIGRATION_0015.forward(django_apps, None)

    # Then the eras are back and the retired parameter is gone again, which is what
    # makes the pair safe to replay rather than a one-way door.
    assert ObligationDueRule.objects.count() == len(MIGRATION_0015.DUE_RULE_ERAS)
    assert not FiscalParameter.objects.filter(key=RETIRED_KEY).exists()
    assert due_rule_for(das(), date(2026, 6, 1)).day == 20


def test_the_live_parameter_registry_omits_exactly_the_retired_key() -> None:
    # Given 0002's two registries
    frozen = {row[0] for row in MIGRATION_0002.PARAMETERS}
    live = {row[0] for row in MIGRATION_0002.LIVE_PARAMETERS}

    # When they are compared
    # Then the live one differs by the retired key alone. The suite's replay fixture
    # routes through the live registry, so a drift here would resurrect the row this
    # migration deletes — and only in the tests that truncate, which is a suite that
    # passes or fails depending on the order its tests happen to run in.
    assert frozen - live == {RETIRED_KEY}
    assert live - frozen == set()

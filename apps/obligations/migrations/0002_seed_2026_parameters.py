"""Seed the seven MEI parameters in force for 2026, each with its citation.

**Only enacted law is seeded.** The ceiling increases to R$ 110.000 (2027) and
R$ 140.000 (2028) proposed by PLP 186/2026, and the R$ 130.000 of PLP 108/2021, are
before the Câmara and are NOT law. Seeding them would tell a firm that a client
billing R$ 95.000 in 2027 is compliant, while Receita Federal desenquadra them
retroactively. `docs/regulatory-watch.md` tracks both and names the review ritual.

**Every row states its category explicitly, NULL included.** NULL means the parameter
applies to every category; a value means it overrides for that one. Leaving a category
unstated would force a guess, and the two guesses resolve differently.

**The proportional monthly rate is per-category, exactly like the ceiling.** Measuring
a caminhoneiro's opening year against R$ 6.750/month rather than R$ 20.966,67 reports
a compliant client as over the limit — and the output is a plausible percentage, so
nothing about it looks wrong.
"""

from datetime import date

from django.apps import apps as global_apps
from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

EFFECTIVE_FROM = date(2026, 1, 1)

GOVBR_MEI = "https://www.gov.br/empresas-e-negocios/pt-br/empreendedor"
LC_123 = "https://www.planalto.gov.br/ccivil_03/leis/lcp/lcp123.htm"
LC_188 = "https://www.planalto.gov.br/ccivil_03/leis/lcp/lcp188.htm"
CGSN_140 = "http://normas.receita.fazenda.gov.br/sijut2consulta/link.action?idAto=92278"

NOTE_CEILING_COMMON = (
    "LC 123/2006 art. 18-A §1º. R$ 81.000,00 since January 2018 (LC 155/2016); "
    "unchanged for 2026."
)
NOTE_CEILING_CAMINHONEIRO = (
    "LC 188/2021, which created MEI-Caminhoneiro with its own substantially higher "
    "annual ceiling. Not an approximation of the common limit."
)
NOTE_PROPORTIONAL_COMMON = (
    "LC 123/2006 art. 18-A §2º: the opening-year limit is the monthly rate times the "
    "months from start of activity to year end, counting a fraction of a month as a "
    "whole month. R$ 81.000,00 / 12."
)
NOTE_PROPORTIONAL_CAMINHONEIRO = (
    "LC 123/2006 art. 18-A §2º applied to the LC 188/2021 caminhoneiro ceiling. "
    "R$ 251.600,00 / 12, rounded to the centavo."
)
NOTE_TOLERANCE = (
    "Resolução CGSN nº 140/2018 art. 115. Excess up to 20% of the ceiling desenquadra "
    "effective 1 January of the FOLLOWING year; above 20% it is retroactive to "
    "1 January of the current year, or to the opening date in the year of opening."
)
NOTE_DUE_DAY = (
    "Resolução CGSN nº 140/2018 art. 40 §3: the DAS is due on the 20th of the month "
    "following the competence, postponed to the next business day when that day is "
    "not one."
)
NOTE_MAX_EMPLOYEES = (
    "LC 123/2006 art. 18-C: a MEI may employ a single worker earning the minimum wage "
    "or the category floor. PLP 186/2026 would raise this to two and is not enacted."
)

# key, mei_category, value, source_url, source_note
PARAMETERS = (
    ("mei.annual_ceiling", "common", "81000.00", GOVBR_MEI, NOTE_CEILING_COMMON),
    (
        "mei.annual_ceiling",
        "caminhoneiro",
        "251600.00",
        LC_188,
        NOTE_CEILING_CAMINHONEIRO,
    ),
    (
        "mei.monthly_proportional",
        "common",
        "6750.00",
        LC_123,
        NOTE_PROPORTIONAL_COMMON,
    ),
    (
        "mei.monthly_proportional",
        "caminhoneiro",
        "20966.67",
        LC_188,
        NOTE_PROPORTIONAL_CAMINHONEIRO,
    ),
    ("mei.excess_tolerance_pct", None, "0.20", CGSN_140, NOTE_TOLERANCE),
    ("das.due_day", None, "20", CGSN_140, NOTE_DUE_DAY),
    ("mei.max_employees", None, "1", LC_123, NOTE_MAX_EMPLOYEES),
)


# `0015` retires this key: the DAS due day belongs to the DAS due rule, and two
# spellings of the same statutory number are one edit away from disagreeing. It stays
# in PARAMETERS above because a migration is frozen history and rewriting `seed` would
# change what a fresh database is recorded as having passed through.
RETIRED_LATER = frozenset({"das.due_day"})

LIVE_PARAMETERS = tuple(row for row in PARAMETERS if row[0] not in RETIRED_LATER)


def _seed_rows(
    apps: app_registry.Apps,
    rows: tuple[tuple[str, str | None, str, str, str], ...],
) -> None:
    parameter_model = apps.get_model("obligations", "FiscalParameter")
    for key, category, value, source_url, source_note in rows:
        parameter_model.objects.update_or_create(
            key=key,
            mei_category=category,
            valid_from=EFFECTIVE_FROM,
            defaults={
                "value": value,
                "valid_to": None,
                "source_url": source_url,
                "source_note": source_note,
            },
        )


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Insert the seven parameters in force for 2026, as this migration first did.

    Frozen: a database migrating from scratch replays history, and `0015` removes the
    retired row a few operations later. Idempotent, keyed on the same triple as the
    unique constraint, whose `nulls_distinct=False` is what stops a re-run from
    duplicating the three category-agnostic rows.
    """
    _seed_rows(apps, PARAMETERS)


def seed_live(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Insert the parameters a fully migrated database holds: the seven minus the one.

    Separate from `seed` because the two answer different questions. `seed` answers
    "what did this migration do?", which history fixes forever; this answers "what
    should the table contain right now?", which every later migration can change.
    Collapsing them would either rewrite history or leave the suite re-seeding a row
    `0015` exists to delete.
    """
    _seed_rows(apps, LIVE_PARAMETERS)


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Remove exactly the rows this migration inserted, and nothing else."""
    parameter_model = apps.get_model("obligations", "FiscalParameter")
    parameter_model.objects.filter(
        key__in={key for key, *_rest in PARAMETERS},
        valid_from=EFFECTIVE_FROM,
    ).delete()


def seed_from_global_registry() -> None:
    """Re-seed using the live app registry, for the test suite's restore fixture.

    A transactional test truncates every table and pytest-django does not replay
    `RunPython`. Left empty, every resolver call would raise `NoEffectiveParameter`
    in whatever test ran next — or worse, a test written to expect that raise would
    pass while asserting nothing about the seeded path.

    Routed through `seed_live`, so the replay reproduces the migrated table rather
    than this migration's own moment in history. Through `seed` it would resurrect
    the row `0015` deletes, and only in the tests that happen to truncate — which is
    a suite that passes or fails depending on the order tests run in.
    """
    seed_live(global_apps, None)


class Migration(migrations.Migration):
    dependencies = [("obligations", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]

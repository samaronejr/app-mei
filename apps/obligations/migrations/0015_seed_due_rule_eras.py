"""Move each deadline rule into its own effective-dated era, and drop the orphan.

**Why the registry is duplicated here rather than imported.** `0003` holds the same
literals, but a migration is frozen history: importing another migration's tuple would
make a future edit to `0003` silently rewrite what this one seeded. The duplication is
the point. It also keeps the quoted obligation codes under `migrations/`, which
`tests/obligations/test_due_rules.py` requires -- that test greps every non-migration
`apps/**/*.py` for the literal `"DASN"` so a `if code == "DASN"` branch cannot appear
unnoticed, and seeding code legitimately names the row it seeds.

**Why the eras open at 2000-01-01 and not at the date this migration ran.** An era is
the window in which the law says the rule applies, not the window in which this table
happened to know about it. Both seeded rules are the regime in force since well before
this product existed -- Resolução CGSN nº 140/2018 restates rules older than itself --
so an era opening in 2026 would leave every earlier competence uncovered, and the
resolver refuses rather than defaults. DASN makes that concrete: it is ANNUAL, so its
deadline lands a full year after the competence it reports on, and a competence of
January 2025 is resolved as-of 2025 against a rule whose window would not yet have
opened. The floor is deliberately far below any competence this product can be asked
about; superseding it when the law actually changes is a pure INSERT of a later era.

**Why `das.due_day` goes.** It was seeded as a fiscal parameter and never read: the
DAS due day lives in the DAS rule, and two spellings of the same statutory number are
one edit away from disagreeing. `0002` keeps seeding it in its frozen historical form
-- migrations are not rewritten -- and its live-registry helper omits it, so the value
is gone from both the migrated database and the test suite's replay path.
"""

from datetime import date

from django.apps import apps as global_apps
from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

CGSN_140 = "http://normas.receita.fazenda.gov.br/sijut2consulta/link.action?idAto=92278"

ERA_OPENS_ON = date(2000, 1, 1)

NOTE_DAS = (
    "Resolução CGSN nº 140/2018 art. 40 §3. The DAS is due on the 20th of the month "
    "following the competence; when that day is not a business day the payment is "
    "postponed to the next one — 'dia útil imediatamente posterior'. Corroborated by "
    "RFB worked examples: Oct 2018 -> 22nd, Dec 2020 -> 21st, Nov 2021 -> 22nd. Note "
    "DAE-MEI, the employee guide, is anticipated BACKWARD; different regime, opposite "
    "direction, which is why direction lives in this column."
)
NOTE_DASN = (
    "Resolução CGSN nº 140/2018 art. 109, confirmed by RFB's official MEI Q&A. The "
    "annual declaration is due 31 May and the deadline does NOT move when that day "
    "falls on a Saturday or Sunday. 2025 confirmed it: 31 May 2025 was a Saturday and "
    "the deadline held."
)

# obligation type code, rule, source note
DUE_RULE_ERAS = (
    ("DAS", {"day": 20, "direction": "forward"}, NOTE_DAS),
    ("DASN", {"month": 5, "day": 31, "direction": "none"}, NOTE_DASN),
)

# The parameter this migration retires, restated exactly as 0002 seeds it so that
# reversing this migration restores the row byte for byte rather than approximately.
ORPHAN_KEY = "das.due_day"
ORPHAN_VALID_FROM = date(2026, 1, 1)
ORPHAN_VALUE = "20"
ORPHAN_NOTE = (
    "Resolução CGSN nº 140/2018 art. 40 §3: the DAS is due on the 20th of the month "
    "following the competence, postponed to the next business day when that day is "
    "not one."
)


def seed_eras(
    apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None
) -> None:
    """Insert one open-ended era per obligation type. Idempotent, for re-runs."""
    due_rule = apps.get_model("obligations", "ObligationDueRule")
    for code, rule, source_note in DUE_RULE_ERAS:
        due_rule.objects.update_or_create(
            obligation_type_id=code,
            valid_from=ERA_OPENS_ON,
            defaults={
                "rule": rule,
                "valid_to": None,
                "source_url": CGSN_140,
                "source_note": source_note,
            },
        )


def forward(apps: app_registry.Apps, editor: BaseDatabaseSchemaEditor | None) -> None:
    """Seed the eras, then retire the parameter they supersede."""
    seed_eras(apps, editor)
    parameter = apps.get_model("obligations", "FiscalParameter")
    parameter.objects.filter(key=ORPHAN_KEY).delete()


def reverse(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Restore the state this migration found: no eras, and the parameter back.

    Written out rather than left to Django, whose default for a `RunPython` with no
    reverse is `IrreversibleError` -- which would make this migration a one-way door
    for no reason, since both halves of it undo cleanly.
    """
    due_rule = apps.get_model("obligations", "ObligationDueRule")
    due_rule.objects.filter(
        obligation_type_id__in=[code for code, *_rest in DUE_RULE_ERAS],
        valid_from=ERA_OPENS_ON,
    ).delete()
    parameter = apps.get_model("obligations", "FiscalParameter")
    parameter.objects.update_or_create(
        key=ORPHAN_KEY,
        mei_category=None,
        valid_from=ORPHAN_VALID_FROM,
        defaults={
            "value": ORPHAN_VALUE,
            "valid_to": None,
            "source_url": CGSN_140,
            "source_note": ORPHAN_NOTE,
        },
    )


def seed_due_rule_eras_from_global_registry() -> None:
    """Re-seed the eras alone, for the test suite's restore fixture.

    Deliberately not `forward`: retiring the orphan is `0002`'s live registry's job,
    and doing it here as well would hide a regression in that split behind this
    fixture, depending on which autouse fixture happened to run first.
    """
    seed_eras(global_apps, None)


class Migration(migrations.Migration):
    dependencies = [("obligations", "0014_obligationduerule")]

    operations = [migrations.RunPython(forward, reverse)]

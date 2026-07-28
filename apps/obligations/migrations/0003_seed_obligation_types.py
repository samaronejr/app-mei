"""Seed the two obligation types and, with them, their deadline rules.

Both rules are settled against the same primary source and are recorded together
because T-040's resolver is required to read direction from data with no code edit —
which is only demonstrable when the data holds two rules that genuinely disagree.

**DAS-MEI rolls FORWARD.** Resolução CGSN nº 140/2018 art. 40 §3: when the 20th is
not a business day the payment is due "até o dia útil imediatamente posterior".
Corroborated by RFB's own worked examples (October 2018 → the 22nd, December 2020 →
the 21st, November 2021 → the 22nd).

**DASN-SIMEI does not roll at all.** Art. 109 fixes it at 31 May and the deadline does
not move for a weekend. 2025 settled this in public: 31 May 2025 was a Saturday and it
held. The anchor document's "last business day of May" is wrong, and encoding it would
push every annual declaration to a date the law does not recognise.
"""

from django.apps import apps as global_apps
from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

CGSN_140 = "http://normas.receita.fazenda.gov.br/sijut2consulta/link.action?idAto=92278"

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

# code, name, periodicity, due_rule, source_note
OBLIGATION_TYPES = (
    (
        "DAS",
        "DAS-MEI — Documento de Arrecadação do Simples Nacional",
        "monthly",
        {"day": 20, "direction": "forward"},
        NOTE_DAS,
    ),
    (
        "DASN",
        "DASN-SIMEI — Declaração Anual do Simples Nacional",
        "annual",
        {"month": 5, "day": 31, "direction": "none"},
        NOTE_DASN,
    ),
)


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Insert both obligation types. Idempotent, for re-runs and for the test suite."""
    obligation_type = apps.get_model("obligations", "ObligationType")
    for code, name, periodicity, due_rule, source_note in OBLIGATION_TYPES:
        obligation_type.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "periodicity": periodicity,
                "due_rule": due_rule,
                "source_url": CGSN_140,
                "source_note": source_note,
            },
        )


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Remove exactly the rows this migration inserted, and nothing else."""
    obligation_type = apps.get_model("obligations", "ObligationType")
    obligation_type.objects.filter(
        code__in=[code for code, *_rest in OBLIGATION_TYPES],
    ).delete()


def seed_from_global_registry() -> None:
    """Re-seed using the live app registry, for the test suite's restore fixture."""
    seed(global_apps, None)


class Migration(migrations.Migration):
    dependencies = [("obligations", "0002_seed_2026_parameters")]

    operations = [migrations.RunPython(seed, unseed)]

"""Every `RunPython` that touches a tenant-scoped table must say so out loud.

`app_migrator` holds `BYPASSRLS`. A data migration is therefore a full cross-tenant
read/write primitive: neither the ORM's ContextVar filter nor the database's
row-level-security policy applies to anything it does. Unlike the two manager escape
hatches the sibling guard covers, there is nothing at the keyboard that looks even
slightly unusual — `RunPython` against a global reference table (the capability matrix,
the holiday calendar, the municipality registry) is routine and correct, and
`RunPython` against `clients_clientcompany` is a script with every firm's data in
scope and no review gate at all. The two are spelled identically.

This is that gate. A migration containing `RunPython` and naming a tenant-scoped
model or its table must carry `# CROSS_TENANT_OK: <reason>`. Today none do, because
all of them touch global tables only — which is the moment to install the control
rather than the moment to skip it.

Detection is two-pronged, because there are exactly two ways a migration reaches a
table: `apps.get_model(...)`, which is the only correct way to reach the historical
model registry, and raw SQL naming `db_table`. The scoped model set is read from
Django's app registry rather than listed here, so a tenant-scoped model added later
is covered without anyone remembering to come back to this file.

The marker is accepted anywhere in the migration, not adjacent to the call. A
migration is one unit of review — the operation and the function it runs must live in
the same file — and the reason belongs with the migration's own docstring, where
someone reading it in a year will actually find it.
"""

import re
from pathlib import Path
from typing import Final

from django.apps import apps as django_apps
from django.db.models import Model

from apps.core.models import TenantScopedModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = PROJECT_ROOT / "apps"

RUN_PYTHON: Final = "migrations.RunPython("
APPROVAL_MARKER: Final = "# CROSS_TENANT_OK:"

# `get_model` in both spellings Django accepts. The model name is matched
# case-insensitively below because the registry is.
GET_MODEL = re.compile(r"""get_model\(\s*["'](\w+)["']\s*,\s*["'](\w+)["']\s*\)""")
GET_MODEL_DOTTED = re.compile(r"""get_model\(\s*["'](\w+)\.(\w+)["']\s*\)""")

SELF = Path(__file__).resolve()

# Probes for the controls below. A guard whose detector silently matches nothing
# passes exactly as loudly as one whose subject is genuinely clean, so the detector
# is exercised in both directions before its verdict is trusted.
SCOPED_BY_MODEL = 'apps.get_model("clients", "ClientCompany")'
SCOPED_BY_DOTTED_MODEL = 'apps.get_model("clients.ClientCompany")'
SCOPED_BY_TABLE = 'schema_editor.execute("UPDATE clients_clientcompany SET x = 1")'
GLOBAL_ONLY = (
    'apps.get_model("authz", "Capability")\n'
    'apps.get_model("fiscal", "Municipality")\n'
    'apps.get_model("obligations", "ObligationType")\n'
    'apps.get_model("clients", "OnboardingItemTemplate")'
)


def _scoped_models() -> list[type[Model]]:
    return [
        model
        for model in django_apps.get_models()
        if issubclass(model, TenantScopedModel)
    ]


def _scoped_labels() -> set[tuple[str, str]]:
    return {
        (model._meta.app_label.lower(), model.__name__.lower())
        for model in _scoped_models()
    }


def _scoped_tables() -> set[str]:
    return {model._meta.db_table for model in _scoped_models()}


def _migration_sources() -> list[Path]:
    return [
        path
        for path in APPS_ROOT.rglob("migrations/*.py")
        if path.resolve() != SELF and "__pycache__" not in path.parts
    ]


def _data_migrations() -> list[Path]:
    return [
        path
        for path in _migration_sources()
        if RUN_PYTHON in path.read_text(encoding="utf-8")
    ]


def _tenant_scoped_references(source: str) -> list[str]:
    labels = _scoped_labels()
    named = {
        f"{app}.{model}"
        for pattern in (GET_MODEL, GET_MODEL_DOTTED)
        for app, model in pattern.findall(source)
        if (app.lower(), model.lower()) in labels
    }
    tabled = {table for table in _scoped_tables() if re.search(rf"\b{table}\b", source)}
    return sorted(named | tabled)


def test_the_scan_finds_the_data_migrations() -> None:
    # Given the migrations tree
    found = _data_migrations()

    # When RunPython operations are located
    # Then real migrations are found. A broken glob would make the guard below pass
    # by scanning nothing at all.
    assert len(found) >= 5
    assert any(path.name == "0002_seed_matrix.py" for path in found)


def test_the_scoped_model_set_is_not_empty() -> None:
    # Given Django's app registry
    tables = _scoped_tables()

    # When tenant-scoped models are collected
    # Then there are some, and they are the ones expected. Derived from the registry
    # rather than listed, so a model added later is covered automatically — but an
    # empty set would make the detector incapable of ever reporting anything.
    assert tables
    assert "clients_clientcompany" in tables
    assert "obligations_obligation" in tables


def test_the_detector_recognises_a_migration_reaching_a_tenant_table() -> None:
    # Given source text in each shape a migration can reach a table by
    # When the detector is applied
    # Then each is recognised. This is the control that keeps the verdict below
    # meaningful: without it, "no offenders" is satisfied by a detector that matches
    # nothing whatsoever.
    assert _tenant_scoped_references(SCOPED_BY_MODEL) == ["clients.ClientCompany"]
    assert _tenant_scoped_references(SCOPED_BY_DOTTED_MODEL) == [
        "clients.ClientCompany",
    ]
    assert _tenant_scoped_references(SCOPED_BY_TABLE) == ["clients_clientcompany"]


def test_the_detector_ignores_a_migration_reaching_only_global_tables() -> None:
    # Given source text touching reference tables that belong to no firm
    # When the detector is applied
    # Then it stays quiet. A detector that fired on the capability matrix would be
    # switched off within a release, which is a slower way of having no gate.
    assert _tenant_scoped_references(GLOBAL_ONLY) == []


def test_every_data_migration_touching_a_tenant_table_is_marked() -> None:
    # Given every migration that runs Python
    offenders = []
    for path in _data_migrations():
        source = path.read_text(encoding="utf-8")
        touched = _tenant_scoped_references(source)
        if touched and APPROVAL_MARKER not in source:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {', '.join(touched)}")

    # When each is checked against the tenant-scoped model set
    # Then none reaches one without saying why. app_migrator holds BYPASSRLS, so
    # such a migration reads and writes across every firm with nothing stopping it.
    assert not offenders, (
        "RunPython migrations touching tenant-scoped tables without a "
        f"'{APPROVAL_MARKER} <reason>' comment:\n  " + "\n  ".join(offenders)
    )

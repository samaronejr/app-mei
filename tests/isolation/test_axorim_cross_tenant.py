"""The Axorim model sweep: every new table, denied on every axis that matters.

T9-T24 added forty-plus models. `test_rls_coverage.py` proves each carries a policy
of the right SHAPE; this module proves the policy BEHAVES, at the row level, per
model: tenant B's seeded row is invisible under tenant A's context, the portal role
reaches exactly the declared tables, a composite foreign key refuses a cross-client
reference, and each table is verified non-empty under `app_test` so no denial above
passed on an empty table.

`transaction=True` throughout, for the same reason as `test_cross_tenant.py`: under a
plain `TestCase` the test already runs inside a transaction, so `SET LOCAL` values
would persist to the enclosing transaction on savepoint RELEASE. It also means
seeding happens per test — the uncommitted graphs a fixture would share are
truncated between tests — which is fine because `seed_model` only materialises the
dependency chain each case actually needs.
"""

from __future__ import annotations

import importlib
from typing import cast

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import FieldDoesNotExist
from django.db import (
    IntegrityError,
    ProgrammingError,
    connection,
    models,
    transaction,
)

from apps.clients.models import ClientCompany
from apps.core.models import TenantScopedModel
from apps.core.tenancy import client_context, tenant_context
from tests.conftest import PORTAL_TABLES
from tests.isolation.axorim_seed import (
    CLIENT_PAIR_ONLY_MODELS,
    NEW_CLIENT_MODELS,
    NEW_OBLIGATION_MODELS,
    SeedContext,
    axorim_models,
    make_seed_context,
    paired_parent_fks,
    paired_parent_targets,
    seed_model,
)
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_ROLE = "app_portal"
FK_VIOLATION = "23503"
PRIVILEGE_DENIED = "42501"

MODELS: list[type[models.Model]] = axorim_models()
SCOPED_MODELS: list[type[models.Model]] = [
    model for model in MODELS if issubclass(model, TenantScopedModel)
]
# Client-scoped models: the swap probe applies to exactly these minus the tables
# whose ONLY composite is the (tenant, client) pair itself, which are decisions
# listed in CLIENT_PAIR_ONLY_MODELS.
CLIENT_SCOPED_MODELS: list[type[models.Model]] = [
    model
    for model in SCOPED_MODELS
    if any(field.name == "client_id" for field in model._meta.fields)
]
CLIENT_SWAP_MODELS: list[type[models.Model]] = [
    model
    for model in CLIENT_SCOPED_MODELS
    if model._meta.db_table not in CLIENT_PAIR_ONLY_MODELS
]


def _unscoped(model: type[models.Model]) -> models.Manager[models.Model]:
    """The manager that bypasses the ContextVar filter, so RLS carries it."""
    manager = model._meta.default_manager
    assert manager is not None
    return cast("models.Manager[models.Model]", cast("object", manager))


@pytest.fixture
def tenants() -> dict[str, SeedContext]:
    """Two firms with flags on; per-test seeding materialises rows on demand."""
    return {
        "alpha": make_seed_context("axr-alpha", "a"),
        "beta": make_seed_context("axr-beta", "b"),
    }


@pytest.fixture
def paired_fks() -> dict[tuple[str, str], str]:
    """The catalog's tenant-composites, read inside a test where DB is allowed."""
    return paired_parent_fks()


def _sibling_client(ctx: SeedContext) -> ClientCompany:
    """A second client in the same firm — what a smuggled reference would name."""
    key = "clients.clientcompany.sibling"
    if key not in ctx.rows:
        with tenant_context(ctx.tenant.id):
            ctx.rows[key] = ClientCompany.all_objects.create(
                tenant=ctx.tenant,
                legal_name=f"SIBLING {ctx.tag.upper()}",
                cnpj="55555555000155" if ctx.tag.endswith("a") else "66666666000166",
                is_mei=True,
            )
    row = ctx.rows[key]
    assert isinstance(row, ClientCompany)
    return row


def test_the_enumeration_covers_every_axorim_model_and_nothing_else() -> None:
    # Given the registry plus the explicit additions lists
    assert_isolated_role()

    # When the swept set is compared against the registry
    labels = {m._meta.label for m in MODELS}
    expected = {
        m._meta.label
        for app in ("cycles", "delivery", "briefing", "automation")
        for m in django_apps.get_app_config(app).get_models()
    }
    expected |= {f"clients.{n}" for n in NEW_CLIENT_MODELS}
    expected |= {f"obligations.{n}" for n in NEW_OBLIGATION_MODELS}

    # Then it is exactly the four new apps' full registry plus the named additions —
    # a model added later without updating the lists fails HERE, named.
    assert labels == expected, (
        "sweep list drifted from the registry; update NEW_CLIENT_MODELS / "
        "NEW_OBLIGATION_MODELS so the addition is enumerated"
    )
    assert len(MODELS) == 44, (
        f"expected 44 swept models as of cd19380, found {len(MODELS)}"
    )


def test_every_swept_model_is_tenant_scoped_or_named_platform() -> None:
    # Given the sweep set
    assert_isolated_role()

    # When the non-scoped members are listed
    unscoped = [m._meta.label for m in MODELS if not issubclass(m, TenantScopedModel)]

    # Then the only one is the platform metrics table (NON_TENANT_TABLES, portal
    # NONE) — anything else is a business table that forgot its tenant boundary.
    assert unscoped == ["automation.EvalRun"], unscoped


@pytest.mark.parametrize("model", SCOPED_MODELS, ids=lambda m: m._meta.label)
def test_rls_hides_tenant_b_rows_under_tenant_a(
    model: type[models.Model],
    tenants: dict[str, SeedContext],
) -> None:
    # Given one seeded row in beta's (tenant, client) pair
    assert_isolated_role()
    beta_row = seed_model(model, tenants["beta"])

    # When tenant A's context reads it by primary key through the UNSCOPED manager —
    # the ORM's ContextVar filter must never carry this assertion
    with tenant_context(tenants["alpha"].tenant.id):
        visible = _unscoped(model).filter(pk=beta_row.pk).count()

    # Then nothing comes back
    assert visible == 0, f"{model._meta.db_table} leaked a row across tenants"


@pytest.mark.parametrize("model", SCOPED_MODELS, ids=lambda m: m._meta.label)
def test_portal_role_access_matches_the_declared_decision(
    model: type[models.Model],
    tenants: dict[str, SeedContext],
) -> None:
    # Given the seeded row, and conftest's allow-list as the declared decision
    assert_isolated_role()
    beta = tenants["beta"]
    seed_model(model, beta)
    table = model._meta.db_table

    # When app_portal reads the table under beta's GUCs — reached through
    # SET LOCAL ROLE from app_runtime, exactly as the middleware does it
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {PORTAL_ROLE}")
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true)", [str(beta.tenant.id)]
        )
        cursor.execute(
            "SELECT set_config('app.client_id', %s, true)", [str(beta.client.id)]
        )
        try:
            cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
            row = cursor.fetchone()
            count = int(row[0]) if row else -1
            refused = None
        except ProgrammingError as error:
            refused = getattr(error.__cause__, "sqlstate", None)
            count = -1
        transaction.set_rollback(True)

    # Then it matches the declaration: allow-listed tables return the seeded row,
    # everything else is stopped by the missing privilege — fail-closed, not empty.
    if table in PORTAL_TABLES:
        assert refused is None, (
            f"{table} is portal-readable but the role was refused (sqlstate={refused})"
        )
        assert count >= 1, f"{table} is portal-readable but the role saw {count} rows"
    else:
        assert refused == PRIVILEGE_DENIED, (
            f"{table} is not portal-declared yet the role read {count} rows "
            f"(sqlstate={refused})"
        )


@pytest.mark.parametrize("model", SCOPED_MODELS, ids=lambda m: m._meta.label)
def test_the_row_exists_for_the_vacuity_check(
    model: type[models.Model],
    tenants: dict[str, SeedContext],
) -> None:
    # Given the seeded row
    assert_isolated_role()
    row = seed_model(model, tenants["beta"])
    table = model._meta.db_table

    # When the table owner (BYPASSRLS) counts it — the ONE place this suite may run
    # unmasked, because "zero visible rows" only means something when rows exist
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        cursor.execute(
            f"SELECT count(*) FROM {table} WHERE id = %s",  # noqa: S608
            [str(row.pk)],
        )
        found = cursor.fetchone()
        count = int(found[0]) if found else -1
        cursor.execute("SET ROLE app_runtime")
        transaction.set_rollback(True)

    # Then the row provably exists, so the denials above are not vacuous
    assert count == 1, f"{table} seed did not land; the denial tests are vacuous"


@pytest.mark.parametrize("model", CLIENT_SWAP_MODELS, ids=lambda m: m._meta.label)
def test_a_paired_fk_rejects_the_cross_client_reference(
    model: type[models.Model],
    tenants: dict[str, SeedContext],
) -> None:
    # Given a child row pointing at alpha's client and alpha's parents
    assert_isolated_role()
    alpha = tenants["alpha"]
    row = seed_model(model, alpha)
    sibling = _sibling_client(alpha)

    # When the row is moved to the OTHER client while its parent composites still
    # name alpha's objects, the *_client_fk pairs must refuse the mismatch
    with (
        tenant_context(alpha.tenant.id),
        pytest.raises(IntegrityError) as exc,
        transaction.atomic(),
    ):
        _unscoped(model).filter(pk=row.pk).update(client_id=sibling.pk)

    assert getattr(exc.value.__cause__, "sqlstate", None) == FK_VIOLATION, (
        f"{model._meta.db_table} accepted a cross-client reference"
    )


def _model_for_table(table: str) -> type[models.Model] | None:
    """The swept model whose db_table is `table`, or None."""
    for candidate in SCOPED_MODELS:
        if candidate._meta.db_table == table:
            return candidate
    return None


def _paired_targets(
    paired: dict[tuple[str, str], str],
) -> dict[str, list[str]]:
    """{table: [parent column]} — composites carrying tenant_id plus a parent."""
    swept = {m._meta.db_table for m in SCOPED_MODELS}
    targets: dict[str, list[str]] = {}
    for table, column in paired:
        if table in swept:
            targets.setdefault(table, []).append(column)
    return targets


@pytest.mark.parametrize("model", SCOPED_MODELS, ids=lambda m: m._meta.label)
def test_paired_fks_reject_cross_tenant_parents(
    model: type[models.Model],
    tenants: dict[str, SeedContext],
    paired_fks: dict[tuple[str, str], str],
) -> None:
    # Given this table's composites (any FK column sharing a key with tenant_id)
    assert_isolated_role()
    table = model._meta.db_table
    parent_columns = _paired_targets(paired_fks).get(table, [])
    if not parent_columns:
        pytest.skip(f"{table} carries no tenant-composite parent foreign key")

    alpha = tenants["alpha"]
    child = seed_model(model, alpha)
    targets = paired_parent_targets()
    for column in parent_columns:
        # The Django FK when the column is a real ORM field; otherwise the
        # constraint's referenced table is the parent (a plain UUID column).
        try:
            field = model._meta.get_field(column.removesuffix("_id"))
        except FieldDoesNotExist:
            field = None
        remote = getattr(field, "remote_field", None)
        parent_model = getattr(remote, "model", None)
        # Only columns that bind the remote table's 'id' are parents: exact-FK
        # twins bind remote 'revision'/'digest' and are not probed.
        remote_table, remote_col = targets.get((table, column), ("", ""))
        if remote_col != "id":
            continue
        if parent_model is None or isinstance(parent_model, str):
            if not remote_table or remote_table == table:
                continue
            parent_model = _model_for_table(remote_table)
        if parent_model is None or parent_model is model:
            continue
        beta_parent = seed_model(parent_model, tenants["beta"])

        # Columns whose update path is guarded so the composite can only be
        # reached on INSERT: bulkitem's immutability trigger fires 23514 on any
        # identity change, and evidencerequest.obligation's CHECK requires
        # kind='comprovante'. For these, a fresh row carrying the cross-tenant
        # parent is the honest probe.
        if (table, column) in _INSERT_PROBE_COLUMNS:
            overrides: dict[str, object] = {column.removesuffix("_id"): beta_parent}
            if table == "cycles_evidencerequest":
                overrides["kind"] = "comprovante"
            with (
                tenant_context(alpha.tenant.id),
                client_context(alpha.client.id),
                pytest.raises((IntegrityError, ProgrammingError)) as exc,
                transaction.atomic(),
            ):
                _clone_insert(model, child, alpha, overrides)
        else:
            # When alpha's row is repointed at beta's parent. Refusal comes from
            # the composite (23503) or, for tenant_id itself, from RLS WITH
            # CHECK (42501) — either way the row cannot escape its tenant.
            with (
                tenant_context(alpha.tenant.id),
                pytest.raises((IntegrityError, ProgrammingError)) as exc,
                transaction.atomic(),
            ):
                _unscoped(model).filter(pk=child.pk).update(**{column: beta_parent.pk})

        assert getattr(exc.value.__cause__, "sqlstate", None) in {
            FK_VIOLATION,
            PRIVILEGE_DENIED,
        }, f"{table}.{column} accepted a cross-tenant parent"


# (table, column) pairs whose UPDATE path is guarded before the composite
# fires, so the cross-tenant probe must run as an INSERT instead.
_INSERT_PROBE_COLUMNS = frozenset(
    {
        ("cycles_bulkitem", "operation_id"),
        ("cycles_evidencerequest", "obligation_id"),
    }
)


def _clone_insert(
    model: type[models.Model],
    child: models.Model,
    ctx: SeedContext,
    overrides: dict[str, object],
) -> models.Model:
    """INSERT a copy of `child` with `overrides` — the guarded-table probe."""
    kwargs: dict[str, object] = {
        field.name: getattr(child, field.name)
        for field in model._meta.fields
        if not field.auto_created and field.name != "id"
    }
    kwargs.update(overrides)
    kwargs.pop("id", None)
    # A row-clone must differ from `child` on whatever makes it unique; the
    # effect_key/position pair is the bulkitem uniqueness an unchanged clone
    # would collide with before the composite fires.
    if "effect_key" in kwargs:
        kwargs["effect_key"] = f"clone-{kwargs['effect_key']}"
    if "position" in kwargs:
        kwargs["position"] = cast("int", kwargs["position"]) + 1
    return _unscoped(model).create(**kwargs)


def test_every_client_scoped_table_is_probed_or_decided(
    paired_fks: dict[tuple[str, str], str],
) -> None:
    # Given the catalog's composites over swept tables
    assert_isolated_role()
    probed = set(_paired_targets(paired_fks)) | {
        m._meta.db_table for m in CLIENT_SWAP_MODELS
    }

    # Then every client-scoped swept table is reached by at least one probe, or is
    # a declared pair-only table. A new table that composites on neither axis
    # fails here named rather than passing unprobed.
    unprobed = [
        m._meta.db_table
        for m in CLIENT_SCOPED_MODELS
        if m._meta.db_table not in probed
        and m._meta.db_table not in CLIENT_PAIR_ONLY_MODELS
    ]
    assert not unprobed, f"client-scoped tables no probe reaches: {unprobed}"


def test_the_client_pair_only_list_is_not_a_blanket(
    paired_fks: dict[tuple[str, str], str],
) -> None:
    # Given the declared pair-only tables
    # Then each is client-scoped AND carries no parent column inside a composite —
    # a name parked here while its schema grew a paired parent would silently
    # exempt the probe it was added to exempt.
    for table in sorted(CLIENT_PAIR_ONLY_MODELS):
        extra = {
            column
            for (tbl, column) in paired_fks
            if tbl == table and column != "client_id"
        }
        assert not extra, (
            f"{table} is declared pair-only but composites reach {sorted(extra)}"
        )


def test_the_checklist_of_scope_suites_from_earlier_todos() -> None:
    # Given the suites T18/T19/T21 shipped for export, briefing and search scope
    required = {
        "tests.isolation.test_evidence_export_scope": "T18 export scope",
        "tests.isolation.test_search_scope": "T21 search scope",
        "tests.briefing.test_scope": "T19 briefing scope",
        "tests.isolation.test_portal_client_isolation": "portal client dimension",
    }

    # When each is imported and its tests counted
    missing: list[str] = []
    for module_name, why in required.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
            continue
        tests = [
            name
            for name in dir(module)
            if name.startswith("test_") and callable(getattr(module, name))
        ]
        if not tests:
            missing.append(f"{module_name} (no test_* functions — {why})")

    # Then all exist and still define cases: a renamed or emptied suite would
    # quietly drop the coverage this sweep asserts is present.
    assert not missing, f"scope suites missing or empty: {missing}"

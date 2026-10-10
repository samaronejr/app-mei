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
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

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
    SeedContext,
    axorim_models,
    client_paired_fk_defs,
    client_paired_tables,
    make_seed_context,
    paired_parent_fks,
    paired_parent_targets,
    runtime_update_denied_tables,
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
# Client-scoped models: rows carrying a client foreign key (field `client`,
# column `client_id`). The swap probe applies to the subset whose table owns a
# (tenant, client, parent) composite — named per-row from the live catalog by
# client_paired_tables(), since parametrize lists cannot see the database.
CLIENT_SCOPED_MODELS: list[type[models.Model]] = [
    model
    for model in SCOPED_MODELS
    if any(field.attname == "client_id" for field in model._meta.fields)
]

# These tables grant UPDATE for state or retention changes, but their identity
# triggers forbid repointing a parent. The composite must be exercised by INSERT.
_INSERT_ONLY_IDENTITY_TABLES = frozenset(
    {
        "cycles_cycleamendment",
        "cycles_completionjustification",
        "cycles_actionproposal",
        "cycles_clientpublication",
    }
)


def _unscoped(model: type[models.Model]) -> models.Manager[models.Model]:
    """The manager that bypasses the ContextVar filter, so RLS carries it."""
    manager = model._base_manager
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
            # ALL_OBJECTS_OK: sibling fixture row under the seed tenant context
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

    # When the swept set is compared against the registry. The additions below
    # are spelled out literally AGAIN — reading them from the constants the seed
    # module defines would make the comparison tautological, and removing an
    # entry from either side of the same constant would pass silently.
    labels = {m._meta.label for m in MODELS}
    expected = {
        m._meta.label
        for app in ("cycles", "delivery", "briefing", "automation")
        for m in django_apps.get_app_config(app).get_models()
    }
    expected |= {
        "clients.ClientContact",
        "clients.Engagement",
        "clients.ImportBatch",
        "clients.ImportRow",
        "clients.TaxProfile",
        "obligations.SourceObservation",
        # AXR1-023's support-grant ledger.
        "tenants.SupportGrant",
    }

    # Then it is exactly the four new apps' full registry plus the named additions —
    # a model added later without updating BOTH lists fails HERE, named.
    assert labels == expected, (
        "sweep list drifted from the registry; update NEW_CLIENT_MODELS / "
        "NEW_OBLIGATION_MODELS / NEW_TENANT_MODELS and the literal set above so "
        "the addition is enumerated"
    )
    assert len(MODELS) == 45, (
        f"expected 45 swept models as of 69f292f, found {len(MODELS)}"
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

    # When tenant A's context reads it by primary key through all_objects — the
    # ContextVar-free manager, so ONLY row-level security stands between the read
    # and beta's row. The scoped `objects` must never carry this assertion: its
    # filter would deny the row even with RLS disabled, which is exactly the
    # vacuity this test exists to rule out.
    with tenant_context(tenants["alpha"].tenant.id):
        scoped = cast("type[TenantScopedModel]", model)
        # ALL_OBJECTS_OK: the RLS assertion must bypass the ORM ContextVar filter;
        # the row-exists control below proves the row is actually in the table.
        visible = scoped.all_objects.filter(pk=beta_row.pk).count()

    # Then nothing comes back. The separate vacuity test reads the seeded row
    # as app_test (BYPASSRLS), proving there is a row for this policy to hide.
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


@pytest.fixture
def client_paired() -> set[str]:
    """Tables owning a (tenant, client, parent) composite, from the catalog."""
    return client_paired_tables()


@pytest.fixture
def client_paired_defs() -> dict[str, list[tuple[str, list[tuple[str, str, str]]]]]:
    """{table: [(name, extras)]} — the client-bearing composites, with bindings."""
    return client_paired_fk_defs()


@pytest.fixture
def append_only() -> set[str]:
    """Tables the runtime role cannot UPDATE — their probes must INSERT."""
    return runtime_update_denied_tables()


def _constraint_name(exc: object) -> str | None:
    """The refusing constraint's name, when Postgres diagnosed one."""
    cause = getattr(exc, "__cause__", exc)
    return getattr(getattr(cause, "diag", None), "constraint_name", None)


def _assert_specific_refusal(
    exc: BaseException,
    *,
    table: str,
    column: str,
    expected_constraint: str | set[str],
) -> None:
    """The refusal must be the named composite firing, not any earlier error.

    SQLSTATE 23503 must name the expected composite. A privilege refusal
    (42501) proves the composite never ran and must fail this assertion.
    """
    sqlstate = getattr(exc.__cause__, "sqlstate", None)
    assert sqlstate == FK_VIOLATION, (
        f"{table}.{column}: refusal came from sqlstate={sqlstate}, expected "
        f"the composite foreign key to fire (23503)"
    )
    expected = (
        expected_constraint
        if isinstance(expected_constraint, set)
        else {expected_constraint}
    )
    assert _constraint_name(exc) in expected, (
        f"{table}.{column} refused with constraint "
        f"{_constraint_name(exc)!r}, expected {sorted(expected)!r}"
    )


@pytest.mark.parametrize("model", SCOPED_MODELS, ids=lambda m: m._meta.label)
def test_a_paired_fk_rejects_the_cross_client_reference(
    model: type[models.Model],
    tenants: dict[str, SeedContext],
    client_paired: set[str],
    client_paired_defs: dict[str, list[tuple[str, list[tuple[str, str, str]]]]],
    append_only: set[str],
) -> None:
    # Given a child row pointing at alpha's client and alpha's parents
    assert_isolated_role()
    table = model._meta.db_table
    if table not in client_paired:
        pytest.skip(f"{table} carries no client-bearing tenant composite")
    alpha = tenants["alpha"]
    row = seed_model(model, alpha)
    sibling = _sibling_client(alpha)

    if (
        table in append_only
        or table in _SWAP_INSERT_PROBES
        or table in _INSERT_ONLY_IDENTITY_TABLES
    ):
        # The composite must fire on INSERT: either the table is append-only for
        # the runtime role (an UPDATE probe would die as 42501 before any FK
        # runs), or its seeded rows hold all-NULL paired columns. A fresh row
        # born mismatched is the honest probe — sqlstate must be 23503 and the
        # named composite must be the one that refused.
        overrides: dict[str, object] = {"client": sibling}
        if table in _SWAP_INSERT_PROBES:
            overrides.update(_SWAP_INSERT_PROBES[table](model, alpha, row))
        with (
            tenant_context(alpha.tenant.id),
            client_context(sibling.id),
            pytest.raises((IntegrityError, ProgrammingError)) as exc,
            transaction.atomic(),
        ):
            _clone_insert(model, row, alpha, overrides)
        _assert_specific_refusal(
            exc.value,
            table=table,
            column="client_id",
            expected_constraint={name for name, _extras in client_paired_defs[table]},
        )
    else:
        # Mutable tables: the UPDATE flips client_id while every composite's
        # parent columns still name alpha's objects. 23503 must name ONE of the
        # table's client-bearing composites — privilege denial proves nothing.
        with (
            tenant_context(alpha.tenant.id),
            pytest.raises((IntegrityError, ProgrammingError)) as exc,
            transaction.atomic(),
        ):
            _unscoped(model).filter(pk=row.pk).update(client_id=sibling.pk)
        names = [n for n, _e in client_paired_defs[table]]
        sqlstate = getattr(exc.value.__cause__, "sqlstate", None)
        assert sqlstate == FK_VIOLATION, (
            f"{table}: expected the composite FK to fire (23503), "
            f"got sqlstate={sqlstate}"
        )
        assert _constraint_name(exc.value) in names, (
            f"{table} refused with {_constraint_name(exc.value)!r}, "
            f"expected one of {names}"
        )


# Tables where the swap must arrive as an INSERT, each with the extra columns a
# usable clone needs: their seeded rows hold all-NULL client-paired columns, so
# only a row born mismatched exercises the composite.
def _agentrun_swap_parent(
    model: type[models.Model], ctx: SeedContext, row: models.Model
) -> dict[str, object]:
    document = seed_model(django_apps.get_model("obligations", "Document"), ctx)
    return {"document": document}


def _taxprofile_swap_parent(
    model: type[models.Model], ctx: SeedContext, row: models.Model
) -> dict[str, object]:
    return {"correction_of": row}


def _deliveryeffect_swap_parent(
    model: type[models.Model], ctx: SeedContext, row: models.Model
) -> dict[str, object]:
    message = seed_model(django_apps.get_model("delivery", "OutboundMessage"), ctx)
    return {"message": message}


_SWAP_INSERT_PROBES: dict[
    str,
    Callable[
        [type[models.Model], SeedContext, models.Model],
        dict[str, object],
    ],
] = {
    "automation_agentrun": _agentrun_swap_parent,
    "clients_taxprofile": _taxprofile_swap_parent,
    "delivery_deliveryeffect": _deliveryeffect_swap_parent,
}


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
    client_paired_defs: dict[str, list[tuple[str, list[tuple[str, str, str]]]]],
    append_only: set[str],
) -> None:
    # Given this table's composites (any FK column sharing a key with tenant_id)
    assert_isolated_role()
    table = model._meta.db_table
    parent_columns = _paired_targets(paired_fks).get(table, [])
    if not parent_columns:
        pytest.skip(f"{table} carries no tenant-composite parent foreign key")

    alpha = tenants["alpha"]
    probe = _ParentProbe(
        alpha=alpha,
        beta=tenants["beta"],
        paired_fks=paired_fks,
        client_paired_defs=client_paired_defs,
        append_only=append_only,
        targets=paired_parent_targets(),
    )
    child = seed_model(model, alpha)
    for column in parent_columns:
        probe.run(model, child, column)


@dataclass
class _ParentProbe:
    """Per-run catalogs for the cross-tenant parent probe."""

    alpha: SeedContext
    beta: SeedContext
    paired_fks: dict[tuple[str, str], str]
    client_paired_defs: dict[str, list[tuple[str, list[tuple[str, str, str]]]]]
    append_only: set[str]
    targets: dict[tuple[str, str], tuple[str, str]]

    def run(self, model: type[models.Model], child: models.Model, column: str) -> None:
        """Probe one composite parent column: repoint or insert at B's parent."""
        table = model._meta.db_table
        parent_model = _parent_model_for(model, column, self.targets)
        if parent_model is None or parent_model is model:
            return
        beta_parent = seed_model(parent_model, self.beta)

        # A beta-side value for the probed column violates every composite that
        # binds (tenant_id, <column>) — the paired constraint plus any
        # client-bearing composite sharing the column. Assert the refusal names
        # one of them, never a bare privilege denial.
        expected = {n for n in [self.paired_fks.get((table, column))] if n}
        expected |= {
            cname
            for cname, extras in self.client_paired_defs.get(table, [])
            if column in {lcol for lcol, _rt, _rc in extras}
        }

        if (
            (table, column) in _INSERT_PROBE_COLUMNS
            or table in self.append_only
            or table in _INSERT_ONLY_IDENTITY_TABLES
        ):
            exc = self._insert_probe(model, child, column, beta_parent)
        else:
            exc = self._update_probe(model, child, column, beta_parent)
        _assert_specific_refusal(
            exc, table=table, column=column, expected_constraint=expected
        )

    def _insert_probe(
        self,
        model: type[models.Model],
        child: models.Model,
        column: str,
        beta_parent: models.Model,
    ) -> BaseException:
        """The INSERT-side probe for columns the update path cannot reach.

        Guarded columns (bulkitem's immutability trigger, evidencerequest's
        CHECK) and every append-only table (the runtime role lacks UPDATE, so
        an update probe dies as 42501 before any FK runs) need a fresh row
        born with the cross-tenant parent.
        """
        overrides: dict[str, object] = {column: beta_parent.pk}
        if model._meta.db_table == "cycles_evidencerequest":
            overrides["kind"] = "comprovante"
        with (
            tenant_context(self.alpha.tenant.id),
            client_context(self.alpha.client.id),
            pytest.raises((IntegrityError, ProgrammingError)) as exc,
            transaction.atomic(),
        ):
            _clone_insert(model, child, self.alpha, overrides)
        return exc.value

    def _update_probe(
        self,
        model: type[models.Model],
        child: models.Model,
        column: str,
        beta_parent: models.Model,
    ) -> BaseException:
        """Repoint alpha's row at beta's parent; the composite must refuse."""
        with (
            tenant_context(self.alpha.tenant.id),
            pytest.raises((IntegrityError, ProgrammingError)) as exc,
            transaction.atomic(),
        ):
            _unscoped(model).filter(pk=child.pk).update(**{column: beta_parent.pk})
        return exc.value


def _parent_model_for(
    model: type[models.Model],
    column: str,
    targets: dict[tuple[str, str], tuple[str, str]],
) -> type[models.Model] | None:
    """The model a composite parent column points at, or None when unprobable.

    The Django FK when the column is a real ORM field; otherwise the
    constraint's referenced table is the parent (a plain UUID column). Only
    columns binding the remote 'id' count as parents: exact-FK twins bind
    remote 'revision'/'digest' and are skipped.
    """
    table = model._meta.db_table
    remote_table, remote_col = targets.get((table, column), ("", ""))
    if remote_col != "id":
        return None
    try:
        field = model._meta.get_field(column.removesuffix("_id"))
    except FieldDoesNotExist:
        field = None
    parent_model = getattr(getattr(field, "remote_field", None), "model", None)
    if parent_model is None or isinstance(parent_model, str):
        if not remote_table or remote_table == table:
            return None
        return _model_for_table(remote_table)
    return cast("type[models.Model]", parent_model)


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
        field.attname: getattr(child, field.attname)
        for field in model._meta.fields
        if not field.auto_created and field.name != "id"
    }
    attnames = {field.name: field.attname for field in model._meta.fields}
    kwargs.update(
        {
            attnames.get(name, name): (
                value.pk if isinstance(value, models.Model) else value
            )
            for name, value in overrides.items()
        }
    )
    kwargs.pop("id", None)
    # A row-clone must differ from `child` on whatever makes it unique; the
    # effect_key/position pair is the bulkitem uniqueness an unchanged clone
    # would collide with before the composite fires.
    if "effect_key" in kwargs:
        kwargs["effect_key"] = f"clone-{kwargs['effect_key']}"
    for column in ("position", "amendment", "number"):
        if column in kwargs:
            kwargs[column] = cast("int", kwargs[column]) + 1
    if "business_intent_key" in kwargs:
        kwargs["business_intent_key"] = f"clone-{kwargs['business_intent_key']}"[:64]

    # Frozen manifests/publications reject mismatched identities BEFORE any FK
    # can run. Isolate that separate layer for this synthetic composite probe:
    # disable only its named user trigger, retain RLS and every FK, then INSERT
    # as app_runtime. The savepoint is always rolled back, including when a
    # broken composite unexpectedly accepts the row, restoring the trigger.
    identity_trigger = {
        "cycles_actionproposal": "cycles_frozen_proposal",
        "cycles_clientpublication": "cycles_frozen_publication",
    }.get(model._meta.db_table)
    with transaction.atomic():
        if identity_trigger is not None:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
                table = connection.ops.quote_name(model._meta.db_table)
                trigger = connection.ops.quote_name(identity_trigger)
                cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
                cursor.execute("SET ROLE app_runtime")
        assert_isolated_role()
        try:
            return _unscoped(model).create(**kwargs)
        finally:
            transaction.set_rollback(True)


@pytest.mark.parametrize(
    ("label", "trigger_name"),
    [
        ("cycles.ActionProposal", "cycles_frozen_proposal"),
        ("cycles.ClientPublication", "cycles_frozen_publication"),
    ],
)
def test_a_successful_clone_restores_the_identity_trigger(
    label: str,
    trigger_name: str,
    tenants: dict[str, SeedContext],
) -> None:
    # Given a valid clone whose composite FKs would allow the INSERT.
    assert_isolated_role()
    model = django_apps.get_model(label)
    alpha = tenants["alpha"]
    row = seed_model(model, alpha)

    # When the probe succeeds rather than raising the expected FK violation.
    with tenant_context(alpha.tenant.id), client_context(alpha.client.id):
        clone = _clone_insert(model, row, alpha, {})

        # Then it still leaves neither a row nor a disabled identity trigger.
        assert not _unscoped(model).filter(pk=clone.pk).exists()
        assert_isolated_role()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tgenabled FROM pg_trigger "
                "WHERE tgname = %s AND tgrelid = %s::regclass",
                [trigger_name, model._meta.db_table],
            )
            assert cursor.fetchone() == ("O",)


def test_every_client_scoped_table_is_probed_or_decided(
    paired_fks: dict[tuple[str, str], str],
    client_paired: set[str],
) -> None:
    # Given the catalog's composites over swept tables
    assert_isolated_role()
    probed = set(_paired_targets(paired_fks)) | client_paired

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

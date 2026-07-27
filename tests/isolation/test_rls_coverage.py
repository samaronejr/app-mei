"""Every tenant table must be policed, and every unpoliced table must be justified.

This is the meta-test that makes the isolation guarantee survive future waves. A new
business table that forgets its `EnableRLS` operation, or a policy weakened to
`FOR SELECT` or to `USING (true)`, turns the build red here rather than shipping.

The inverse direction matters just as much: a table with no `tenant_id` and no
allow-list entry is a gap nobody would otherwise notice.
"""

from fnmatch import fnmatchcase

import pytest
from django.apps import apps as django_apps
from django.db import connection, models

from apps.core.models import TenantScopedModel
from apps.core.rls import NON_TENANT_TABLES, is_exempt_from_tenant_policy
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)


def _tenant_scoped_models() -> list[type[models.Model]]:
    return [
        model
        for model in django_apps.get_models()
        if issubclass(model, TenantScopedModel) and not model._meta.abstract
    ]


def _tenant_scoped_tables() -> set[str]:
    return {model._meta.db_table for model in _tenant_scoped_models()}


def _model_id(model: type[models.Model]) -> str:
    return model.__name__


EVERY_TENANT_MODEL = pytest.mark.parametrize(
    "model",
    _tenant_scoped_models(),
    ids=_model_id,
)


def _all_public_tables() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "ORDER BY tablename",
        )
        return [row[0] for row in cursor.fetchall()]


def _rls_flags(table: str) -> tuple[bool, bool]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = %s AND relnamespace = 'public'::regnamespace",
            [table],
        )
        row = cursor.fetchone()
    assert row is not None, f"{table} is not in pg_class"
    return (bool(row[0]), bool(row[1]))


def _policies(table: str) -> list[tuple[str, str, str | None, str | None]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT policyname, cmd, qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = %s ORDER BY policyname",
            [table],
        )
        return [(r[0], r[1], r[2], r[3]) for r in cursor.fetchall()]


def test_there_is_at_least_one_tenant_scoped_model() -> None:
    # Given the installed apps
    # When tenant-scoped models are enumerated
    # Then the set is non-empty, so every parametrized assertion below actually runs
    assert _tenant_scoped_models()


def test_the_role_guard_holds_for_this_module_too() -> None:
    # Given a database test in this package
    # When the effective role is checked
    # Then it is the unprivileged one
    current_user, session_user = assert_isolated_role()
    assert current_user == "app_runtime"
    assert session_user == "app_test"


@EVERY_TENANT_MODEL
def test_row_level_security_is_enabled_and_forced(model: type[models.Model]) -> None:
    # Given a concrete tenant-scoped model
    assert_isolated_role()
    table = model._meta.db_table

    # When its pg_class flags are read
    enabled, forced = _rls_flags(table)

    # Then both are set. ENABLE without FORCE leaves the table owner — which is who
    # migrations run as — reading and writing every tenant's rows.
    assert enabled is True, f"{table} does not have ENABLE ROW LEVEL SECURITY"
    assert forced is True, f"{table} does not have FORCE ROW LEVEL SECURITY"


@EVERY_TENANT_MODEL
def test_the_policy_shape_is_not_merely_present(model: type[models.Model]) -> None:
    # Given a concrete tenant-scoped model
    assert_isolated_role()
    table = model._meta.db_table

    # When its policies are read
    policies = _policies(table)

    # Then at least one exists...
    assert policies, f"{table} has no row-level-security policy"

    # ...and its SHAPE is checked, not merely its existence. A bare "≥1 policy"
    # assertion passes for a FOR SELECT-only policy that leaves INSERT and UPDATE
    # completely unguarded, and for a permissive USING (true) added later to "fix" a
    # broken screen.
    for name, cmd, qual, with_check in policies:
        assert cmd == "ALL", f"{table}.{name} covers only {cmd}, not every command"
        assert qual is not None, f"{table}.{name} has no USING expression"
        assert with_check is not None, f"{table}.{name} has no WITH CHECK expression"
        assert qual.strip().lower() != "true", f"{table}.{name} USING is permissive"
        assert with_check.strip().lower() != "true", (
            f"{table}.{name} WITH CHECK is permissive"
        )
        assert "NULLIF" in qual.upper(), (
            f"{table}.{name} would raise 22P02 on an empty GUC"
        )
        assert ", true)" in qual, f"{table}.{name} omits current_setting's missing_ok"


@EVERY_TENANT_MODEL
def test_the_tenant_column_is_not_nullable(model: type[models.Model]) -> None:
    # Given a concrete tenant-scoped model
    assert_isolated_role()
    table = model._meta.db_table

    # When the tenant column's nullability is read
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "AND column_name = 'tenant_id'",
            [table],
        )
        row = cursor.fetchone()

    # Then it is NOT NULL. A nullable tenant_id would compare NULL to the GUC, which
    # is never true — the row would become invisible to everyone including its owner.
    assert row is not None, f"{table} has no tenant_id column"
    assert row[0] == "NO", f"{table}.tenant_id is nullable"


def test_every_table_is_either_policed_or_explicitly_allow_listed() -> None:
    # Given every table in the database
    assert_isolated_role()
    scoped = _tenant_scoped_tables()
    tables = _all_public_tables()
    assert tables, "no tables found — this check would pass vacuously"

    # When each is classified
    unaccounted = [
        table
        for table in tables
        if table not in scoped and not is_exempt_from_tenant_policy(table)
    ]

    # Then none is unaccounted for. A new business table with no tenant_id and no
    # allow-list entry is a gap that is otherwise invisible.
    assert not unaccounted, (
        "tables that are neither tenant-scoped nor allow-listed in "
        f"apps.core.rls.NON_TENANT_TABLES: {unaccounted}"
    )


def test_the_allow_list_is_evaluated_with_fnmatch_not_membership() -> None:
    # Given the allow-list, which contains glob patterns
    # When a globbed name is tested both ways
    # Then a plain `in` test fails where fnmatchcase succeeds. Using `in` would turn
    # this suite red on django_migrations, auth_user and accounts_user immediately.
    assert "django_migrations" not in NON_TENANT_TABLES
    assert is_exempt_from_tenant_policy("django_migrations")
    assert any(
        fnmatchcase("django_migrations", pattern) for pattern in NON_TENANT_TABLES
    )


def test_no_tenant_scoped_table_is_also_allow_listed() -> None:
    # Given the tenant-scoped tables and the allow-list
    assert_isolated_role()

    # When they are intersected
    both = [
        table
        for table in _tenant_scoped_tables()
        if is_exempt_from_tenant_policy(table)
    ]

    # Then no table is in both. An over-broad glob would silently exempt a real
    # business table from the inverse check above.
    assert not both, f"tenant-scoped tables wrongly matched by the allow-list: {both}"


def test_the_tenancy_root_tables_are_deliberately_unpoliced() -> None:
    # Given the three tables the middleware must read before any context exists
    assert_isolated_role()
    roots = ["tenants_tenant", "tenants_membership", "tenants_invite"]

    # When their policies are counted
    policed = [table for table in roots if _policies(table)]

    # Then none is policed. Policing them would make the membership lookup return zero
    # rows under the fail-closed predicate, and nobody could sign in.
    assert policed == []
    assert all(is_exempt_from_tenant_policy(table) for table in roots)

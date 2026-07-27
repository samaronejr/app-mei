"""`EnableRLS` must produce a forced, fail-closed, both-directions policy.

Each assertion here maps to a way the policy can look present while doing nothing:
`ENABLE` without `FORCE` (owner bypasses), `USING` without `WITH CHECK` (writes into
another tenant are accepted), `current_setting` without `missing_ok` (raises instead of
denying), and `::uuid` without `NULLIF` (raises 22P02 on a recycled connection).
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection

from apps.core.migrations._operations import TENANT_PREDICATE, EnableRLS
from apps.core.rls import NON_TENANT_TABLES, is_exempt_from_tenant_policy
from apps.core.tests.models import ExampleTenantModel

pytestmark = pytest.mark.django_db

TABLE = ExampleTenantModel._meta.db_table


def _rls_flags(table: str) -> tuple[bool, bool]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = %s AND relnamespace = 'public'::regnamespace",
            [table],
        )
        row = cursor.fetchone()
    assert row is not None, f"{table} not found in pg_class"
    return (row[0], row[1])


def _policies(table: str) -> list[tuple[str, str, str | None, str | None]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT policyname, cmd, qual, with_check FROM pg_policies "
            "WHERE tablename = %s AND schemaname = 'public' ORDER BY policyname",
            [table],
        )
        return [(r[0], r[1], r[2], r[3]) for r in cursor.fetchall()]


def _isolation_expressions(table: str) -> tuple[str, str]:
    """Return (USING, WITH CHECK), failing loudly if either is absent."""
    (_, _, qual, with_check), *_ = _policies(table)
    assert qual is not None, f"{table} policy has no USING expression"
    assert with_check is not None, f"{table} policy has no WITH CHECK expression"
    return qual, with_check


def test_row_level_security_is_enabled_and_forced() -> None:
    # Given the migrated fixture table
    # When its pg_class flags are read
    enabled, forced = _rls_flags(TABLE)

    # Then both are set. ENABLE alone is not enough: migrations create the table as its
    # owner, and owners bypass row-level security until FORCE is also set.
    assert enabled is True
    assert forced is True


def test_the_table_carries_exactly_one_isolation_policy() -> None:
    # Given the migrated fixture table
    # When its policies are listed
    policies = _policies(TABLE)

    # Then there is one, named after the table
    assert len(policies) == 1
    assert policies[0][0] == f"{TABLE}_tenant_isolation"


def test_the_policy_covers_every_command_not_just_select() -> None:
    # Given the isolation policy
    (_, cmd, _, _), *_ = _policies(TABLE)

    # When its command scope is read
    # Then it is ALL — a SELECT-only policy leaves INSERT and UPDATE unguarded while
    # still satisfying a naive "at least one policy exists" check
    assert cmd == "ALL"


def test_the_policy_guards_reads_and_writes_alike() -> None:
    # Given the isolation policy
    (_, _, qual, with_check), *_ = _policies(TABLE)

    # When both expressions are read
    # Then neither is missing. Without WITH CHECK a tenant can write a row *into*
    # another tenant, which USING alone does not prevent.
    assert qual is not None
    assert with_check is not None


def test_neither_policy_expression_is_permissive() -> None:
    # Given the isolation policy
    qual, with_check = _isolation_expressions(TABLE)

    # When the expressions are compared against a blanket allow
    # Then neither is the literal `true`, which would re-open the table completely
    assert qual.strip().lower() != "true"
    assert with_check.strip().lower() != "true"


def test_the_policy_uses_the_missing_ok_form_of_current_setting() -> None:
    # Given the isolation policy as PostgreSQL parsed it
    qual, with_check = _isolation_expressions(TABLE)

    # When the rendered expressions are inspected
    # Then both pass missing_ok, so an unset GUC denies rather than raising
    assert ", true)" in qual
    assert ", true)" in with_check


def test_the_policy_normalizes_the_empty_string_before_casting() -> None:
    # Given the isolation policy as PostgreSQL parsed it
    qual, with_check = _isolation_expressions(TABLE)

    # When the rendered expressions are inspected
    # Then NULLIF is present. A pooled connection and every production login leave the
    # GUC as '', and ''::uuid raises SQLSTATE 22P02 rather than denying access.
    assert "NULLIF" in qual.upper()
    assert "NULLIF" in with_check.upper()


def test_the_predicate_template_encodes_all_three_traps() -> None:
    # Given the shared predicate template
    predicate = TENANT_PREDICATE.format(column="tenant_id")

    # When it is rendered
    # Then it carries missing_ok, the empty-string guard, and the uuid cast
    assert "current_setting('app.tenant_id', true)" in predicate
    assert "NULLIF(" in predicate
    assert "::uuid" in predicate


def test_the_allow_list_is_matched_by_glob_not_membership() -> None:
    # Given the allow-list, which contains glob patterns
    # When a globbed table name is tested both ways
    # Then plain membership fails and fnmatch succeeds — this is why the coverage
    # meta-test must use fnmatchcase or it turns red on django_migrations
    assert "auth_user" not in NON_TENANT_TABLES
    assert is_exempt_from_tenant_policy("auth_user")
    assert is_exempt_from_tenant_policy("django_migrations")
    assert is_exempt_from_tenant_policy("accounts_user")
    assert is_exempt_from_tenant_policy("tenants_membership")


def test_the_tenant_scoped_fixture_table_is_not_allow_listed() -> None:
    # Given the fixture table, which is genuinely tenant-scoped
    # When it is tested against the allow-list
    # Then it is absent — an over-broad pattern here would hide a missing policy
    assert not is_exempt_from_tenant_policy(TABLE)


def test_the_operation_reports_itself_reversible_and_serializable() -> None:
    # Given the operation
    operation = EnableRLS("ExampleTenantModel")

    # When it is deconstructed for the migration writer
    name, args, kwargs = operation.deconstruct()

    # Then it round-trips, and Django knows it can be reversed
    assert operation.reversible is True
    assert name == "EnableRLS"
    assert args == []
    assert kwargs == {"model_name": "ExampleTenantModel"}
    assert EnableRLS(*args, **kwargs).model_name == "ExampleTenantModel"


@pytest.mark.django_db(transaction=True)
def test_migrating_backwards_drops_the_policy_cleanly() -> None:
    # Given the applied migration, reversed as the table owner (app_runtime may not
    # ALTER TABLE, which is itself part of the role split)
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        # When core_tests is migrated back to the migration before EnableRLS
        call_command("migrate", "core_tests", "0001", verbosity=0, stdout=StringIO())

        # Then the policy is gone and row-level security is lifted
        assert _policies(TABLE) == []
        assert _rls_flags(TABLE) == (False, False)

        # And re-applying it restores both
        call_command("migrate", "core_tests", verbosity=0, stdout=StringIO())
        assert len(_policies(TABLE)) == 1
        assert _rls_flags(TABLE) == (True, True)
    finally:
        call_command("migrate", "core_tests", verbosity=0, stdout=StringIO())
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")

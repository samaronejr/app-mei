"""The role split is what makes every row-level-security policy in this schema real.

If the application connects as a superuser, as the table owner, or as anything holding
BYPASSRLS, every policy is silently inert: no error, no warning, and every tenant sees
every row. These assertions are the guard against that, and they are deliberately
about the *connection*, not about any model.
"""

import pytest
from django.db import ProgrammingError, connection, transaction

pytestmark = pytest.mark.django_db


def _scalar_row(sql: str) -> tuple[object, ...]:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    assert row is not None
    return tuple(row)


def test_app_runtime_is_neither_superuser_nor_bypassrls() -> None:
    # Given the role the application connects as
    # When its cluster-level privilege flags are read
    flags = _scalar_row(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'app_runtime'",
    )

    # Then it can neither ignore policies nor own its way around them
    assert flags == (False, False)


def test_assertions_run_as_app_runtime_not_as_the_login_role() -> None:
    # Given a database test, which connects as app_test and then assumes app_runtime
    # When the effective and login identities are compared
    current_user, session_user = _scalar_row("SELECT current_user, session_user")

    # Then RLS is evaluated as app_runtime even though the login role is app_test
    assert current_user == "app_runtime"
    assert session_user == "app_test"


def test_the_effective_role_carries_no_privilege_escape() -> None:
    # Given whatever role the assertions are actually running as
    # When that role's own flags are read back
    flags = _scalar_row(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
    )

    # Then it is unprivileged — this is what stops a vacuous cross-tenant pass
    assert flags == (False, False)


def test_the_connection_never_runs_as_the_bootstrap_superuser() -> None:
    # Given the configured default connection
    # When the effective role is read
    (current_user,) = _scalar_row("SELECT current_user")

    # Then it is not the cluster superuser the postgres image creates
    assert current_user != "postgres"
    assert current_user != "app_mei"


def test_app_migrator_holds_bypassrls_so_backfills_see_every_row() -> None:
    # Given the role that runs migrations
    # When its flags are read
    flags = _scalar_row(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'app_migrator'",
    )

    # Then it bypasses RLS without being a superuser, so a RunPython backfill under
    # FORCE ROW LEVEL SECURITY cannot silently touch zero rows
    assert flags == (False, True)


def test_app_test_may_assume_the_runtime_role() -> None:
    # Given the login role used by the suite
    # When its memberships are enumerated
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_has_role('app_test', 'app_runtime', 'MEMBER'),
                   pg_has_role('app_test', 'app_migrator', 'MEMBER')
            """,
        )
        row = cursor.fetchone()

    # Then SET ROLE app_runtime is permitted — without this the whole suite dies on
    # its first statement with "permission denied to set role"
    assert row == (True, True)


def test_app_runtime_cannot_create_tables() -> None:
    # Given the unprivileged application role
    # When it attempts to create a table
    # Then PostgreSQL refuses, proving it is not the schema owner
    with (
        pytest.raises(ProgrammingError),
        transaction.atomic(),
        connection.cursor() as (cursor),
    ):
        cursor.execute("CREATE TABLE runtime_should_not_create (id integer)")


def test_the_runtime_role_owns_no_table() -> None:
    # Given every table in the public schema
    # When their owners are listed
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM pg_tables
            WHERE schemaname = 'public' AND tableowner = 'app_runtime'
            """,
        )
        row = cursor.fetchone()
    assert row is not None

    # Then app_runtime owns none of them — owners bypass RLS unless FORCE is set, so
    # ownership would be a second, quieter way to defeat every policy
    assert row[0] == 0

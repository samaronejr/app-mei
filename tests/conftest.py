"""Test-suite wiring for the database role split.

Two things happen here, and both are load-bearing for every isolation assertion in
this project:

1. After migrations, `app_runtime` is granted DML on the freshly created test tables.
   Neither `ALTER DEFAULT PRIVILEGES` nor the main database's ACLs reach them — test
   databases are built from `template1` and their tables are owned by `app_test` — so
   without this the suite dies with `permission denied` before reaching a policy.
2. Every database test runs its assertions under `SET ROLE app_runtime`. The login
   role is `app_test`, which owns the tables and holds `BYPASSRLS`; leaving it in place
   would let every cross-tenant test pass while reading every tenant's rows.
"""

from collections.abc import Iterator

import pytest
from django.db import connection
from pytest_django import DjangoDbBlocker

RUNTIME_ROLE = "app_runtime"


@pytest.fixture(scope="session")
def django_db_setup(
    django_db_setup: None,
    django_db_blocker: DjangoDbBlocker,
) -> None:
    """Grant the runtime role access to the tables migrations just created.

    `ALTER DEFAULT PRIVILEGES` only affects objects created *afterwards*, so the
    statement in `ops/sql/roles.sql` is inert for tables that already exist. The
    explicit `ON ALL TABLES` grant below is what actually lands.
    """
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}",
        )
        cursor.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}",
        )
        # The blanket grant above would silently restore UPDATE/DELETE on the
        # append-only audit table, so T-018's revoke is re-applied as the last
        # statement. Guarded on existence because audit_event arrives in Wave 3.
        cursor.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('public.audit_event') IS NOT NULL THEN
                    REVOKE UPDATE, DELETE ON audit_event FROM app_runtime;
                END IF;
            END
            $$
            """,
        )


def _wants_database(request: pytest.FixtureRequest) -> bool:
    if request.node.get_closest_marker("django_db") is not None:
        return True
    return bool({"db", "transactional_db"} & set(request.fixturenames))


@pytest.fixture(autouse=True)
def _assume_runtime_role(request: pytest.FixtureRequest) -> Iterator[None]:
    """Run each database test's assertions as the unprivileged application role.

    Requesting `db` rather than declaring it as a parameter keeps non-database tests
    off the database entirely, while still ordering this fixture *after* pytest-django
    has flushed and *before* it flushes again on teardown. That ordering is required:
    `TransactionTestCase` truncates as the current role, and `app_runtime` cannot
    truncate tables owned by `app_test`.
    """
    if not _wants_database(request):
        yield
        return

    request.getfixturevalue("db")
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {RUNTIME_ROLE}")
    try:
        yield
    finally:
        if connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")

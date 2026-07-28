"""The audit trail must be unrewritable — including by the roles that own it.

Revoking `UPDATE` and `DELETE` from `app_runtime` stops the web process and nothing
else. `app_migrator` and `app_test` **own** these tables and hold `BYPASSRLS`, so a
migration, a fixture, or anyone holding the migration credentials could rewrite an
investigation's evidence and leave no trace. The trigger is what binds them, and the
cases below assert both halves separately so that losing either one is visible.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from django.db import Error as DatabaseError
from django.db import connection, transaction

from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

AUDIT_TABLES = ("audit_event", "audit_platformevent")


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


def _seed(tenant: Tenant) -> None:
    with tenant_context(tenant.id):
        Event.objects.create(
            tenant=tenant,
            action=AuditAction.EXPORT,
            object_type="ClientCompany",
            object_id="1",
        )
    PlatformEvent.objects.create(action=AuditAction.LOGIN_FAILED, subject="a@b.example")


@contextmanager
def _as_owner() -> Iterator[None]:
    """Drop the suite's SET ROLE so statements run as the table OWNER.

    Entered OUTSIDE the transaction under test, so the role is restored after that
    transaction has already rolled back — issuing SET ROLE on an aborted transaction
    would raise and mask the error being asserted.
    """
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


def _execute(sql: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql)


def _assert_refused(sql: str) -> None:
    """Assert a statement is rejected with SQLSTATE 42501, insufficient_privilege.

    Django re-raises psycopg's exception wrapped in its own, so the precise SQLSTATE
    is only visible on __cause__. Asserting the wrapper alone would also accept a
    syntax error, which would make every denial below pass for the wrong reason.
    """
    with pytest.raises(DatabaseError) as caught:
        _execute(sql)
    assert isinstance(caught.value.__cause__, psycopg.errors.InsufficientPrivilege), (
        f"expected insufficient_privilege, got {caught.value.__cause__!r}"
    )


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_the_runtime_role_may_insert_and_select(table: str, tenant: Tenant) -> None:
    # Given the unprivileged application role
    assert_isolated_role()
    _seed(tenant)

    # When the table is read back inside the tenant context that wrote it — outside
    # it, audit_event correctly returns nothing and this control would be vacuous
    with tenant_context(tenant.id), connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        row = cursor.fetchone()

    # Then the row it just wrote is there. Without this positive control, the denial
    # cases below would pass on a table nothing can write to at all.
    assert row is not None
    assert row[0] > 0


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_the_runtime_role_cannot_update(table: str, tenant: Tenant) -> None:
    # Given a written record
    assert_isolated_role()
    _seed(tenant)

    # When the application role tries to rewrite it
    # Then the grant refuses
    _assert_refused(f"UPDATE {table} SET action = 'tampered'")  # noqa: S608


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_the_runtime_role_cannot_delete(table: str, tenant: Tenant) -> None:
    # Given a written record
    assert_isolated_role()
    _seed(tenant)

    # When the application role tries to erase it
    # Then the grant refuses
    _assert_refused(f"DELETE FROM {table}")  # noqa: S608


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_the_runtime_role_cannot_truncate(table: str, tenant: Tenant) -> None:
    # Given a written record
    assert_isolated_role()
    _seed(tenant)

    # When the application role tries to TRUNCATE
    # Then it is refused. TRUNCATE is a separate privilege, is documented as NOT
    # subject to row-level security, and fires no row triggers — a blanket GRANT ALL
    # anywhere in the fixtures would hand it back and break append-only invisibly.
    _assert_refused(f"TRUNCATE {table}")


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_even_the_table_owner_cannot_update(table: str, tenant: Tenant) -> None:
    # Given a written record and the OWNING role, which also holds BYPASSRLS
    assert_isolated_role()
    _seed(tenant)

    # When the owner tries to rewrite history
    # Then the trigger refuses. This is the half that grants cannot cover.
    with _as_owner():
        _assert_refused(f"UPDATE {table} SET action = 'tampered'")  # noqa: S608


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_even_the_table_owner_cannot_delete(table: str, tenant: Tenant) -> None:
    # Given a written record and the owning role
    assert_isolated_role()
    _seed(tenant)

    # When the owner tries to erase it
    # Then the trigger refuses
    with _as_owner():
        _assert_refused(f"DELETE FROM {table}")  # noqa: S608


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_the_grants_are_enumerated_not_blanket(table: str) -> None:
    # Given the privileges actually recorded for the application role
    assert_isolated_role()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = 'public' AND table_name = %s AND grantee = %s "
            "ORDER BY privilege_type",
            [table, "app_runtime"],
        )
        granted = {row[0] for row in cursor.fetchall()}

    # When they are compared with what append-only permits
    # Then only INSERT and SELECT are held — no UPDATE, DELETE, TRUNCATE or REFERENCES
    assert granted == {"INSERT", "SELECT"}, f"{table} grants: {sorted(granted)}"


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_the_append_only_trigger_exists(table: str) -> None:
    # Given the table's triggers
    assert_isolated_role()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = %s AND NOT t.tgisinternal",
            [table],
        )
        triggers = {row[0] for row in cursor.fetchall()}

    # When the append-only trigger is looked for
    # Then it is present. A grant-only design would leave this set empty.
    assert f"{table}_append_only" in triggers

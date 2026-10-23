"""AXRM-007: `integrations.0001` reverses while empty and refuses once a row exists.

Plan A11 and the todo's rollout: "recovery = revert while the tables are empty,
afterwards flag off with the tables kept (forward repair)". The reverse is replayed as
the migration authority (RESET ROLE: app_test, BYPASSRLS like app_migrator), so the
row count the guard reads is every firm's rows, not the zero a policy would show.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.integrations.models import ProviderConnection
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

TABLES = ("integrations_providerconnection", "integrations_clientproviderlink")


def _tables() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r'",
            [list(TABLES)],
        )
        return {row[0] for row in cursor.fetchall()}


@contextmanager
def _as_migrator() -> Iterator[None]:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


def test_the_empty_tables_reverse_and_come_back_isolated() -> None:
    with _as_migrator():
        MigrationExecutor(connection).migrate([("integrations", None)])
        assert _tables() == set()

    assert _tables() == set(TABLES)
    with connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = %s",
                [table],
            )
            assert cursor.fetchone() == (True, True)
            cursor.execute(
                "SELECT has_table_privilege('app_portal', %s, 'SELECT')", [table]
            )
            assert cursor.fetchone() == (False,)
            cursor.execute(
                "SELECT has_table_privilege('app_runtime', %s, privilege) "
                "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) privilege",
                [table],
            )
            assert cursor.fetchall() == [(True,)] * 4


def test_a_populated_table_refuses_the_reverse_and_keeps_its_rows() -> None:
    tenant = Tenant.objects.create(name="Populada", slug="populada")
    with tenant_context(tenant.pk):
        ProviderConnection.objects.create(
            tenant=tenant, provider="serpro", environment="fake"
        )

    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate([("integrations", None)])

    assert _tables() == set(TABLES)
    with tenant_context(tenant.pk):
        assert ProviderConnection.objects.count() == 1

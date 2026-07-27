"""Primary keys must be time-ordered, and every tenant index must lead with tenant_id.

The index-order rule is not cosmetic. Every row-level-security predicate in this schema
filters on `tenant_id`, and PostgreSQL can only serve that predicate from a B-tree
whose *first* column is `tenant_id`. An index that leads with anything else forces a
sequential scan on the hottest access path in the product.
"""

import uuid

import pytest
from django.apps import apps as django_apps
from django.db import connection, models, transaction

from apps.core.models import TenantScopedModel, UUIDv7PrimaryKeyModel
from apps.core.tests.models import ExampleTenantModel
from apps.tenants.models import Tenant


def _concrete_tenant_scoped_models() -> list[type[models.Model]]:
    return [
        model
        for model in django_apps.get_models()
        if issubclass(model, TenantScopedModel) and not model._meta.abstract
    ]


def _index_columns(index_name: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
            WHERE ic.relname = %s
            ORDER BY k.ord
            """,
            [index_name],
        )
        return [row[0] for row in cursor.fetchall()]


def _multi_column_indexes(table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ic.relname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_class ic ON ic.oid = i.indexrelid
            WHERE c.relname = %s AND array_length(i.indkey, 1) > 1
            ORDER BY ic.relname
            """,
            [table],
        )
        return [row[0] for row in cursor.fetchall()]


def test_uuid7_is_available_and_reports_version_7() -> None:
    # Given the uuid6 backport, since Python 3.13's stdlib has no uuid7()
    import uuid6  # noqa: PLC0415

    # When a key is generated
    generated = uuid6.uuid7()

    # Then it is a version-7 UUID
    assert isinstance(generated, uuid.UUID)
    assert generated.version == 7


def test_successive_uuid7_values_sort_ascending_as_strings() -> None:
    # Given a batch of keys generated in order
    import uuid6  # noqa: PLC0415

    generated = [str(uuid6.uuid7()) for _ in range(50)]

    # When they are compared as strings
    # Then they are already ascending — this is the time-ordering property that keeps
    # index inserts local instead of scattering them across the B-tree like uuid4
    assert generated == sorted(generated)


@pytest.mark.django_db(transaction=True)
def test_tenant_scoped_rows_get_a_version_7_primary_key() -> None:
    # Given a tenant, and that tenant established as the transaction's context.
    # The context is mandatory: the forced WITH CHECK policy rejects an insert made
    # with no app.tenant_id, so this cannot be written without it.
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant.id)])

        # When a scoped row is created
        row = ExampleTenantModel.all_objects.create(tenant=tenant, name="first")

    # Then its primary key is a UUIDv7, inherited from the abstract base
    assert row.id.version == 7


def test_the_abstract_bases_create_no_table_of_their_own() -> None:
    # Given the abstract bases
    # When their metadata is read
    # Then both are abstract, so only concrete subclasses reach the database
    assert TenantScopedModel._meta.abstract is True
    assert UUIDv7PrimaryKeyModel._meta.abstract is True


def test_every_concrete_tenant_scoped_model_is_discoverable() -> None:
    # Given the installed apps under the test settings
    discovered = _concrete_tenant_scoped_models()

    # When the fixture model is looked for
    # Then it is present — the meta-tests in T-007b and T-014 iterate this same set,
    # so an empty set would make all of them pass vacuously
    assert ExampleTenantModel in discovered
    assert len(discovered) >= 1


@pytest.mark.django_db
def test_every_composite_index_on_a_tenant_table_leads_with_tenant_id() -> None:
    # Given every concrete tenant-scoped table
    models_under_test = _concrete_tenant_scoped_models()
    assert models_under_test

    checked = 0
    for model in models_under_test:
        table = model._meta.db_table
        composite = _multi_column_indexes(table)
        assert composite, f"{table} has no composite index leading with tenant_id"

        # When each multi-column index is expanded in column order
        for index_name in composite:
            columns = _index_columns(index_name)

            # Then tenant_id is the first column, or the RLS predicate cannot use it
            assert columns[0] == "tenant_id", (
                f"index {index_name} on {table} leads with {columns[0]!r}, "
                f"not 'tenant_id' (columns: {columns})"
            )
            checked += 1

    assert checked >= 1


@pytest.mark.django_db
def test_the_tenant_foreign_key_is_protected_against_cascade_deletion() -> None:
    # Given the tenant foreign key on a scoped model
    field = ExampleTenantModel._meta.get_field("tenant")

    # When its deletion behaviour is read
    # Then deleting a tenant is refused while scoped rows remain, rather than silently
    # cascading away a firm's entire accounting history
    assert field.remote_field.on_delete is models.PROTECT

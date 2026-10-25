"""AXRM-014: `obligations.0024` round-trips while empty and refuses once populated.

Plan A11 and the todo's rollout: "new tables empty; recovery = revert while empty,
flag off afterwards". The reverse runs as the migration authority (RESET ROLE:
app_test, BYPASSRLS like app_migrator), so the refusal counts every firm's rows.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import (
    FederalConsultation,
    FederalPayment,
    Obligation,
)
from apps.tenants.models import Tenant
from tests.integrations.test_migration_rollback import _as_migrator

pytestmark = pytest.mark.django_db(transaction=True)

TABLES = (
    "obligations_federalconsultation",
    "obligations_federalpayment",
    "obligations_fiscalsituationreport",
)
BEFORE = ("obligations", "0023_document_portal_visibility")
CNPJS = ("12ABC34501DE35", "11222333000181")


def _tables() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r'",
            [list(TABLES)],
        )
        return {row[0] for row in cursor.fetchall()}


def _firm(index: int) -> tuple[Tenant, ClientCompany, Obligation]:
    tenant = Tenant.objects.create(name=f"Firma {index}", slug=f"federal-rt-{index}")
    with tenant_context(tenant.pk):
        client = ClientCompany.objects.create(
            tenant=tenant, legal_name=f"Cliente {index}", cnpj=CNPJS[index]
        )
        obligation = Obligation.objects.create(
            tenant=tenant,
            client=client,
            obligation_type_id="DAS",
            competence_month=date(2026, 9, 1),
            nominal_due_date=date(2026, 10, 20),
            resolved_due_date=date(2026, 10, 20),
        )
    return tenant, client, obligation


def _consultation(tenant: Tenant, client: ClientCompany) -> FederalConsultation:
    with tenant_context(tenant.pk):
        return FederalConsultation.objects.create(
            tenant=tenant,
            client=client,
            kind="pagamentos",
            range_start=date(2026, 9, 1),
            range_end=date(2026, 10, 31),
            consulted_at=datetime(2026, 10, 25, 9, tzinfo=UTC),
            outcome="nada_localizado",
            environment="fake",
        )


def _snapshot(firms: list[tuple[Tenant, ClientCompany, Obligation]]) -> list[object]:
    rows: list[object] = []
    for tenant, _client, _obligation in firms:
        with tenant_context(tenant.pk):
            rows.append(list(Obligation.objects.order_by("pk").values()))
    return rows


def test_populated_two_tenant_round_trip_keeps_rows_and_restores_every_control() -> (
    None
):
    # Given two firms with pre-existing obligation rows and empty federal tables
    firms = [_firm(0), _firm(1)]
    before = _snapshot(firms)

    # When the schema goes back to 0023 and forward again
    with _as_migrator():
        MigrationExecutor(connection).migrate([BEFORE])
        assert _tables() == set()
        assert _snapshot(firms) == before
        leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
        MigrationExecutor(connection).migrate(leaves)

    # Then the existing rows are untouched and every isolation control is back
    assert _tables() == set(TABLES)
    assert _snapshot(firms) == before
    with connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = %s",
                [table],
            )
            assert cursor.fetchone() == (True, True), table
            cursor.execute(
                "SELECT has_table_privilege('app_portal', %s, 'SELECT')", [table]
            )
            assert cursor.fetchone() == (False,), table
            cursor.execute(
                "SELECT has_table_privilege('app_runtime', %s, 'UPDATE')", [table]
            )
            assert cursor.fetchone() == (table != "obligations_federalpayment",)
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = ANY(%s::regclass[]) "
            "AND NOT tgisinternal ORDER BY tgname",
            [list(TABLES)],
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "obligations_federalpayment_append_only",
            "obligations_fiscalsituationreport_private_document",
        ]

    # And the recreated tables work and stay tenant-isolated
    (tenant_a, client_a, obligation_a), (tenant_b, _client_b, _obligation_b) = firms
    receipt = _consultation(tenant_a, client_a)
    with tenant_context(tenant_a.pk):
        payment = FederalPayment.objects.create(
            tenant=tenant_a,
            client=client_a,
            consultation=receipt,
            numero_documento="07202609000001",
            tipo="DAS",
            periodo_apuracao=date(2026, 9, 1),
            data_arrecadacao=date(2026, 10, 15),
            receita_codigo="1007",
            valor_total=Decimal("76.60"),
            matched_obligation=obligation_a,
        )
        # The runtime grant is restored as SELECT/INSERT only (the trigger itself is
        # asserted above and owner-tested in test_federal_records.py).
        with (
            pytest.raises(DatabaseError, match="permission denied"),
            transaction.atomic(),
        ):
            FederalPayment.objects.filter(pk=payment.pk).delete()
    with tenant_context(tenant_b.pk):
        assert FederalConsultation.objects.count() == 0
        assert FederalPayment.objects.count() == 0


def test_a_populated_federal_table_refuses_the_reverse_and_keeps_its_rows() -> None:
    # Given one receipt in the SECOND firm only
    firms = [_firm(0), _firm(1)]
    tenant_b, client_b, _obligation = firms[1]
    receipt = _consultation(tenant_b, client_b)

    # When the schema owner attempts the reverse
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate([BEFORE])

    # Then nothing was dropped and the receipt is still there
    assert _tables() == set(TABLES)
    with tenant_context(tenant_b.pk):
        assert FederalConsultation.objects.get().pk == receipt.pk

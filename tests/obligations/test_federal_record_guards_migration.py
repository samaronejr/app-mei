"""Populate the pre-repair schema, retain its two-tenant evidence, refuse loss."""

from datetime import UTC, date, datetime

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.obligations.models import Document, FederalConsultation, FiscalSituationReport
from tests.integrations.test_migration_rollback import _as_migrator
from tests.obligations.test_federal_records_migration import _firm

pytestmark = pytest.mark.django_db(transaction=True)
BEFORE_REPAIR = ("obligations", "0024_federal_records")


def test_populated_legacy_scopes_and_report_privacy_survive_forward_repair() -> None:
    # Given two firms' receipts and private report originals in the old schema.
    firms = [_firm(0), _firm(1)]
    receipt_ids = []
    document_ids = []
    with _as_migrator():
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        executor.migrate([BEFORE_REPAIR])
        historical = executor.loader.project_state([BEFORE_REPAIR]).apps
        old_receipt = historical.get_model("obligations", "FederalConsultation")
        old_document = historical.get_model("obligations", "Document")
        old_report = historical.get_model("obligations", "FiscalSituationReport")
        for tenant, client, _obligation in firms:
            receipt = old_receipt._base_manager.create(
                tenant_id=tenant.pk,
                client_id=client.pk,
                kind="situacao_fiscal",
                consulted_at=datetime(2026, 10, 25, 9, tzinfo=UTC),
                outcome="encontrado",
                environment="fake",
            )
            document = old_document._base_manager.create(
                tenant_id=tenant.pk,
                client_id=client.pk,
                sha256="d" * 64,
                original_filename="synthetic-legacy-report.pdf",
                content_type="application/pdf",
                byte_size=1,
                uploaded_by_id=None,
                client_upload=False,
                portal_visible=False,
                acquisition_origin="provedor_federal",
                acquisition_receipt_ref="synthetic-legacy-call",
            )
            old_report._base_manager.create(
                tenant_id=tenant.pk,
                client_id=client.pk,
                consultation_id=receipt.pk,
                document_id=document.pk,
                requested_at=receipt.consulted_at,
                state="emitido",
            )
            receipt_ids.append(receipt.pk)
            document_ids.append(document.pk)

        # When the additive repair is applied over those populated records.
        MigrationExecutor(connection).migrate(leaves)

    # Then all rows survive with distinct durable, tenant-scoped scope digests.
    scopes = []
    for index, (tenant, _client, _obligation) in enumerate(firms):
        with tenant_context(tenant.pk):
            receipt = FederalConsultation.objects.get(pk=receipt_ids[index])
            scopes.append(receipt.scope_digest)
            assert len(receipt.scope_digest) == 64
            report = FiscalSituationReport.objects.get()
            assert report.document_id == document_ids[index]
            assert report.document_portal_visible is False
            with pytest.raises(DatabaseError), transaction.atomic():
                Document.objects.filter(pk=document_ids[index]).update(
                    portal_visible=True
                )
    assert scopes[0] != scopes[1]

    # And reversal refuses before either the scopes or privacy key can be lost.
    with _as_migrator(), pytest.raises(IrreversibleError, match="forward repair"):
        MigrationExecutor(connection).migrate([BEFORE_REPAIR])
    for index, (tenant, _client, _obligation) in enumerate(firms):
        with tenant_context(tenant.pk):
            assert FederalConsultation.objects.get().scope_digest == scopes[index]
            assert (
                FiscalSituationReport.objects.get().document_id == document_ids[index]
            )


def test_database_refuses_invalid_durable_scopes_and_forged_privacy_keys() -> None:
    # Given a real durable receipt and a report with a database-generated key.
    tenant, client, _obligation = _firm(0)
    with tenant_context(tenant.pk):
        receipt = FederalConsultation.objects.create(
            tenant=tenant,
            client=client,
            kind="pagamentos",
            range_start=date(2026, 9, 1),
            range_end=date(2026, 10, 31),
            consulted_at=datetime(2026, 10, 25, 9, tzinfo=UTC),
            outcome="nada_localizado",
            environment="fake",
            scope_digest="a" * 64,
        )
        report = FiscalSituationReport.objects.create(
            tenant=tenant,
            client=client,
            consultation=receipt,
            requested_at=receipt.consulted_at,
        )
        # When raw SQL attempts to forge either stored invariant.
        with (
            pytest.raises(DatabaseError, match="federalconsultation_scope_digest"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE obligations_federalconsultation "
                "SET scope_digest = '' WHERE id = %s",
                [receipt.pk],
            )
        with (
            pytest.raises(DatabaseError, match="generated column"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE obligations_fiscalsituationreport "
                "SET document_portal_visible = TRUE WHERE id = %s",
                [report.pk],
            )
        # Then the database-owned key and durable digest remain intact.
        receipt.refresh_from_db()
        report.refresh_from_db()
        assert receipt.scope_digest == "a" * 64
        assert report.document_portal_visible is False

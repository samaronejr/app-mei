"""The corrected policy preserves populated intake metadata in both directions."""

from pathlib import Path

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState
from apps.obligations.models import Document
from apps.tenants.models import Tenant
from tests.obligations.ingest_fixtures import ingest_setup, portal_cursor

pytestmark = pytest.mark.django_db(transaction=True)


def test_populated_two_tenant_intakes_survive_reverse_and_reapply(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    # Given visible legacy intake metadata in two independent tenants.
    setup = ingest_setup(settings, tmp_path)
    other = Tenant.objects.create(name="Other intake firm", slug="other-intake-firm")
    expected = []
    for tenant in (setup.graph.tenant, other):
        with tenant_context(tenant.pk):
            client = (
                setup.graph.client
                if tenant.pk == setup.graph.tenant.pk
                else ClientCompany.objects.create(
                    tenant=tenant, legal_name="Roundtrip client", cnpj="11444777000161"
                )
            )
            document = Document.objects.create(
                tenant=tenant,
                client=client,
                uploaded_by=setup.graph.actor,
                sha256="a" * 64,
                original_filename="legacy.pdf",
                content_type="application/pdf",
                byte_size=1,
            )
            intake = DocumentIntake.objects.create(
                tenant=tenant,
                client=client,
                document=document,
                state=IntakeState.VERIFIED,
            )
            expected.append((intake.pk, tenant.pk, client.pk, document.pk))
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        # When the actual migration is reversed with populated metadata.
        MigrationExecutor(connection).migrate(
            [("obligations", "0022_schedulerheartbeat_job_outcome")]
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, tenant_id, client_id, document_id "
                "FROM cycles_documentintake ORDER BY id"
            )
            assert cursor.fetchall() == expected
            cursor.execute(
                "SELECT qual FROM pg_policies "
                "WHERE policyname = 'cycles_documentintake_portal_client_isolation'"
            )
            assert "EXISTS" not in cursor.fetchone()[0]
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, tenant_id, client_id, document_id "
                "FROM cycles_documentintake ORDER BY id"
            )
            assert cursor.fetchall() == expected
            cursor.execute(
                "SELECT qual FROM pg_policies "
                "WHERE policyname = 'cycles_documentintake_portal_client_isolation'"
            )
            assert "EXISTS" in cursor.fetchone()[0]
        # Then every original survives and client scoping is live after reapplication.
        with portal_cursor(setup) as cursor:
            cursor.execute("SELECT id FROM cycles_documentintake")
            assert cursor.fetchall() == [(expected[0][0],)]
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")

"""Migration replay and import rollback preserve private shadow receipts."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.automation.models import AgentRun
from apps.clients.importing import (
    RollbackRefusedError,
    commit_import,
    plan_import,
    rollback_import,
)
from apps.clients.models import ClientCompany, ImportBatch, ImportStatus
from apps.core.tenancy import tenant_context
from tests.ui.factories import client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


def test_migration_replay_preserves_runtime_access_and_portal_denial() -> None:
    # Given the real schema, replayed as the migration authority.
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        MigrationExecutor(connection).migrate([("automation", None)])
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")

    # When runtime access is inspected after recreation, not session bootstrap.
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("app_runtime",)
        for table in ("automation_agentrun", "automation_evalrun"):
            cursor.execute(
                "SELECT has_table_privilege('app_runtime', %s, privilege) "
                "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) privilege",
                [table],
            )
            # Then every required runtime operation remains available.
            assert cursor.fetchall() == [(True,)] * 4
            cursor.execute(
                "SELECT has_table_privilege('app_portal', %s, 'SELECT')", [table]
            )
            assert cursor.fetchone() == (False,)


@pytest.mark.parametrize("shadow_rows", [0, 1])
def test_import_rollback_refuses_only_existing_shadow_receipts(
    shadow_rows: int,
) -> None:
    # Given an imported client with either no receipt or one durable reservation.
    firm = make_firm("ai-rollback")
    cnpj = make_cnpj(client_base(0, "ai-rollback"))
    upload = SimpleUploadedFile(
        "clients.csv", f"CNPJ;Razão social\n{cnpj};Cliente Sintetico MEI".encode()
    )
    with tenant_context(firm.tenant.pk):
        plan = plan_import(firm.owner, upload, None)
        commit_import(firm.owner, plan.batch_id, expected_version=plan.version)
        client = ClientCompany.objects.get(import_batch_id=plan.batch_id)
        if shadow_rows:
            AgentRun.objects.create(
                tenant=firm.tenant,
                client=client,
                input_hash="a" * 64,
                model_id="fake-v1",
                reserved_brl=Decimal("0.025"),
                expires_at=timezone.now() + timedelta(days=365),
            )

        # When rollback is requested, only actual work may prevent deletion.
        if shadow_rows:
            with pytest.raises(RollbackRefusedError) as refusal:
                rollback_import(firm.owner, plan.batch_id, expected_version=2)
            assert [
                (item.kind, item.client_id, item.count)
                for item in refusal.value.dependents
            ] == [("automation.agentrun", client.pk, 1)]
            batch = ImportBatch.objects.get(pk=plan.batch_id)
            assert (batch.status, batch.version) == (ImportStatus.COMMITTED, 2)
            assert AgentRun.objects.get().reserved_brl == Decimal("0.025")
        else:
            result = rollback_import(firm.owner, plan.batch_id, expected_version=2)
            assert result.deleted == 1
            assert not ClientCompany.objects.filter(pk=client.pk).exists()

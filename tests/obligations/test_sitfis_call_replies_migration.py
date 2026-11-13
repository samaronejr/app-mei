"""The SITFIS call ledger upgrades pending reports and refuses receipt loss."""

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.core.tenancy import tenant_context
from apps.obligations.models import FiscalSituationReport
from tests.integrations.test_migration_rollback import _as_migrator
from tests.obligations.test_federal_records_migration import _firm

pytestmark = pytest.mark.django_db(transaction=True)
PREVIOUS = ("obligations", "0027_sitfis_reply_record")
# AXRM-020 is the current leaf; this test still exercises the 0028 ledger upgrade.
CURRENT = ("obligations", "0029_fiscal_register")


def test_the_call_ledger_round_trip_and_populated_reverse_guard() -> None:
    tenant, client, _obligation = _firm(0)
    with _as_migrator():
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        assert CURRENT in leaves
        executor.migrate([PREVIOUS])
        historical = executor.loader.project_state([PREVIOUS]).apps
        receipt = historical.get_model(
            "obligations", "FederalConsultation"
        )._base_manager.create(
            tenant_id=tenant.pk,
            client_id=client.pk,
            kind="situacao_fiscal",
            scope_digest="a" * 64,
            consulted_at=datetime(2026, 11, 4, tzinfo=UTC),
            outcome="incerto",
            environment="fake",
        )
        old = historical.get_model(
            "obligations", "FiscalSituationReport"
        )._base_manager.create(
            tenant_id=tenant.pk,
            client_id=client.pk,
            consultation_id=receipt.pk,
            requested_at=receipt.consulted_at,
            wait_until=datetime(2026, 11, 4, 12, tzinfo=UTC),
            reply_call="synthetic-previous-call",
            reply_wait_ms=300_000,
            state="aguardando",
        )
        MigrationExecutor(connection).migrate(leaves)
        # Empty new ledgers reverse and apply again without changing the old wait.
        MigrationExecutor(connection).migrate([PREVIOUS])
        MigrationExecutor(connection).migrate(leaves)
    with tenant_context(tenant.pk):
        report = FiscalSituationReport.objects.get(pk=old.pk)
        assert report.call_replies == {}
        assert report.reply_wait_ms == 300_000
        assert report.wait_until == datetime(2026, 11, 4, 12, tzinfo=UTC)
        ledger = {
            "synthetic-paid-call": [
                {"received_at": "2026-11-04T12:00:00+00:00", "wait_ms": 300_000}
            ]
        }
        report.call_replies = ledger
        report.save(update_fields=["call_replies"])
    with (
        _as_migrator(),
        pytest.raises(IrreversibleError, match="forward repair"),
    ):
        MigrationExecutor(connection).migrate([PREVIOUS])
    with tenant_context(tenant.pk):
        assert FiscalSituationReport.objects.get(pk=old.pk).call_replies == ledger

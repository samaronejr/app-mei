"""Raw bulk deletes cannot bypass holds or discard durable recovery descriptors."""

from pathlib import Path

import pytest
from django.core.files.storage import default_storage
from django.db import DatabaseError, connection, transaction
from psycopg import sql
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.export_records import EvidenceExport, ExportState
from apps.cycles.export_service import request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.retention import place_hold
from tests.cycles.evidence_fixtures import scenario_a

pytestmark = pytest.mark.django_db(transaction=True)


def test_populated_provenance_hold_and_attempt_rows_refuse_raw_delete(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    scenario = scenario_a(settings, tmp_path, "retained-provenance")
    setup = scenario.setup
    with tenant_context(setup.graph.tenant.pk):
        setup.graph.client.refresh_from_db()
        place_hold(
            setup.graph.actor,
            setup.graph.client.pk,
            reason="Preservar a evidência",
            expected_version=setup.graph.client.version,
        )
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    build_export(str(setup.graph.tenant.pk), str(export.pk))
    with tenant_context(setup.graph.tenant.pk), connection.cursor() as cursor:
        export = EvidenceExport.objects.get(pk=export.pk)
        assert export.state == ExportState.READY
        for table in (
            "cycles_documentintake",
            "cycles_extractedfield",
            "cycles_review",
            "cycles_internalnote",
            "cycles_requestinternal",
            "cycles_exceptioninternal",
            "cycles_legalhold",
            "cycles_evidenceexport",
            "cycles_exportattempt",
        ):
            count_sql = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            cursor.execute(count_sql)
            before = cursor.fetchone()
            assert before is not None, table
            assert before[0] > 0, table
            with (
                pytest.raises(DatabaseError, match="retained evidence metadata"),
                transaction.atomic(),
            ):
                cursor.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
            cursor.execute(count_sql)
            assert cursor.fetchone() == before
    assert default_storage.exists(export.storage_key)

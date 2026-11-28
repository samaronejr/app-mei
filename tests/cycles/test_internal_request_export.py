"""System requests keep truthful attribution and valid V1 archive bytes."""

import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from django.core.files.storage import default_storage
from jsonschema import Draft202012Validator, FormatChecker
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.display_index import display_source_digest
from apps.cycles.display_reads import load_cycle_detail
from apps.cycles.export_records import ExportState
from apps.cycles.export_service import request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.index_projection import source_digest
from apps.cycles.services import requests
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)
V1_SCHEMA = (
    Path(__file__).resolve().parents[2] / "apps/cycles/schemas/evidence_index_v1.json"
)
SYSTEM_ATTRIBUTION = "00000000-0000-0000-0000-000000000000"


def _export(setup: PortalSetup) -> str:
    tenant_id = setup.graph.tenant.pk
    with tenant_context(tenant_id):
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    build_export(str(tenant_id), str(export.pk))
    with tenant_context(tenant_id):
        export.refresh_from_db()
        assert export.state == ExportState.READY, export.error_code
    return export.storage_key


def _index_bytes(storage_key: str) -> bytes:
    with default_storage.open(storage_key) as handle, ZipFile(handle) as archive:
        return archive.read("index.json")


def test_system_requests_export_with_uuid_attribution_in_shipped_v1(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("request-v1-export")
    validator = Draft202012Validator(
        json.loads(V1_SCHEMA.read_text()), format_checker=FormatChecker()
    )
    legacy_key = _export(setup)
    legacy = _index_bytes(legacy_key)
    validator.validate(json.loads(legacy))
    ensure = getattr(requests, "ensure_internal_request", None)
    assert ensure is not None, "No way to create a system-attributed request."
    with tenant_context(setup.graph.tenant.pk):
        system = ensure(setup.cycle, "documentos_fiscais_do_periodo")
        assert system.created_by_id is None

    new = json.loads(_index_bytes(_export(setup)))
    validator.validate(new)
    assert new["schema"] == "axorim.evidence.v1"
    creators = {row["id"]: row["created_by_id"] for row in new["requests"]}
    assert creators == {
        str(setup.request.pk): str(setup.graph.actor.pk),
        str(system.pk): SYSTEM_ATTRIBUTION,
    }
    assert _index_bytes(legacy_key) == legacy
    with tenant_context(setup.graph.tenant.pk):
        reads = load_cycle_detail(setup.graph.actor, setup.cycle.pk)
        assert display_source_digest(reads) == source_digest(reads.cycle)

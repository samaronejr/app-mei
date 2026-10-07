"""Populated client and tenant controls for both evidence rows and actual ZIP bytes."""

import hashlib
import json
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from django.db import connection
from django.http import Http404, StreamingHttpResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientAssignment, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.archive_response import ArchiveDescriptor, EvidenceArchiveResponse
from apps.cycles.evidence_index import evidence_index
from apps.cycles.export_records import EvidenceExport, ExportState
from apps.cycles.export_service import load_export, request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.models import EvidenceRequest
from apps.tenants.middleware import TenantMiddleware
from apps.tenants.models import Membership, TenantRole
from tests.cycles.document_fixtures import fixture
from tests.cycles.evidence_fixtures import complete_evidence_scenario, scenario_a
from tests.portal.request_fixtures import portal_setup
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


def test_actual_bundle_excludes_populated_other_client_and_tenant(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.MEDIA_ROOT = tmp_path
    own_scenario = scenario_a(settings, tmp_path, "export-scope-own")
    foreign_scenario = scenario_a(settings, tmp_path, "export-scope-foreign")
    own, foreign = own_scenario.setup, foreign_scenario.setup
    other_user = add_member(
        own.graph.tenant,
        "other@export-scope.test",
        TenantRole.CLIENT_OWNER,
        client=own.other_client,
    )
    with tenant_context(own.graph.tenant.pk):
        other_contact = ClientContact.objects.create(
            tenant=own.graph.tenant,
            client=own.other_client,
            name="Outro responsável",
            email=other_user.email,
            membership=Membership.objects.get(user=other_user, tenant=own.graph.tenant),
            channel_email_allowed=True,
        )
        ClientAssignment.objects.create(
            tenant=own.graph.tenant, client=own.other_client, user=own.graph.staff
        )
        other = replace(
            own,
            graph=replace(own.graph, client=own.other_client),
            cycle=own.other_request.cycle,
            request=own.other_request,
            portal_user=other_user,
            contact=other_contact,
        )
    other_scenario = complete_evidence_scenario(other)
    controls = [own_scenario.index, other_scenario.index, foreign_scenario.index]
    for setup, index in zip((own, other, foreign), controls, strict=True):
        assert [row["id"] for row in index.data["requests"]] == [str(setup.request.pk)]
        for section in (
            "documents",
            "reviews",
            "fields",
            "proposals",
            "approvals",
            "messages",
            "provider_responses",
            "observations",
            "acknowledgements",
        ):
            assert index.data[section], section
    with tenant_context(own.graph.tenant.pk):
        # The fixture is non-vacuous even before the export boundary is exercised.
        assert (
            EvidenceRequest.objects.filter(
                client_id__in=[own.graph.client.pk, own.other_client.pk]
            ).count()
            == 2
        )
        own.cycle.refresh_from_db()
        export = request_export(
            own.graph.actor, own.cycle.pk, expected_version=own.cycle.version
        )
    build_export(str(own.graph.tenant.pk), str(export.pk))
    with tenant_context(own.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY
        key = export.storage_key
        with pytest.raises(Http404):
            evidence_index(own.graph.actor, own.cycle.pk, index_id=controls[1].pk)
        with pytest.raises(Http404):
            evidence_index(own.graph.actor, own.cycle.pk, index_id=controls[2].pk)
    with zipfile.ZipFile(tmp_path / key) as archive:
        index_bytes = archive.read("index.json")
        data = json.loads(index_bytes)
        original_id = controls[0].data["documents"][0]["id"]
        payload = fixture("nfe_emitente_cliente.xml").replace(
            b"11222333000181", own.graph.client.cnpj.encode()
        )
        assert [row["id"] for row in data["documents"]] == [original_id]
        assert archive.read(f"originals/{original_id}.xml") == payload
        html_bytes = archive.read("index.html")
        manifest = archive.read("manifest.sha256")
        for setup, index in ((other, controls[1]), (foreign, controls[2])):
            foreign_id = index.data["documents"][0]["id"]
            assert str(setup.graph.client.pk).encode() not in index_bytes
            assert str(setup.request.pk).encode() not in index_bytes
            for section in (
                "documents",
                "reviews",
                "fields",
                "proposals",
                "approvals",
                "messages",
                "provider_responses",
                "observations",
                "acknowledgements",
            ):
                for row in index.data[section]:
                    assert row["id"].encode() not in index_bytes
                    assert row["id"].encode() not in html_bytes
            assert f"originals/{foreign_id}.xml" not in archive.namelist()
            assert foreign_id.encode() not in manifest


def test_unassigned_staff_cannot_read_or_request_export(
    settings: SettingsWrapper,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = portal_setup("export-unassigned")
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        assert load_export(setup.graph.actor, export.pk).pk == export.pk
        setup.cycle.refresh_from_db()
        before = EvidenceExport.objects.count()
        with pytest.raises(Http404):
            load_export(setup.graph.staff, export.pk)
        with pytest.raises(Http404):
            request_export(
                setup.graph.staff,
                setup.cycle.pk,
                expected_version=setup.cycle.version,
            )
        assert EvidenceExport.objects.count() == before
    assert (
        setup.firm(setup.graph.staff)
        .get(f"/evidencias/arquivos/{export.pk}/baixar/")
        .status_code
        == 404
    )
    assert setup.portal().get(f"/evidencias/arquivos/{export.pk}/").status_code == 404


def test_generic_streaming_is_rejected_even_on_the_export_route() -> None:
    setup = portal_setup("export-streaming")
    request = RequestFactory().get(f"/evidencias/arquivos/{setup.cycle.pk}/baixar/")
    with pytest.raises(TypeError, match="streaming response"):
        TenantMiddleware._reject_streaming(
            StreamingHttpResponse(iter([b"not an authorized archive"])),
            setup.graph.tenant,
            request,
        )
    response = EvidenceArchiveResponse(
        ArchiveDescriptor("unused", "archive.zip", 0, "0" * 64)
    )
    with pytest.raises(TypeError, match="streaming response"):
        TenantMiddleware._reject_streaming(
            response,
            setup.graph.tenant,
            RequestFactory().get(f"/ciclos/{setup.cycle.pk}/"),
        )
    response.close()


def test_download_iterator_has_zero_queries_and_closes_on_disconnect(
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    body = b"bounded archive" * 10000
    (tmp_path / "archive.zip").write_bytes(body)
    response = EvidenceArchiveResponse(
        ArchiveDescriptor(
            "archive.zip", "archive.zip", len(body), hashlib.sha256(body).hexdigest()
        )
    )
    with CaptureQueriesContext(connection) as queries:
        iterator = response.streaming_content
        assert isinstance(iterator, Iterator)
        assert next(iterator) == body[:65536]
        response.close()
        assert list(iterator) == []
    assert len(queries) == 0

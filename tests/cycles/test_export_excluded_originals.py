"""Unsafe originals are named, not opened, in otherwise usable evidence exports."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from django.core.files import File
from django.core.files.storage import default_storage
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.completion_checks import completion_items
from apps.cycles.evidence import IntakeState, LinkOrigin, ScanVerdict
from apps.cycles.export_records import ExportState
from apps.cycles.export_service import request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.models import (
    CycleState,
    CycleTask,
    DocumentIntake,
    EvidenceLink,
    ReviewDecision,
    ServiceCycle,
    TaskState,
)
from apps.delivery.tasks import dispatch_tenant
from apps.obligations.models import Document
from tests.browser.journeys import smtp_at
from tests.cycles.completion_fixtures import CompletionSetup, close_cycle
from tests.cycles.document_fixtures import fixture, reviewed, submit
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup, portal_setup

pytestmark = pytest.mark.django_db(transaction=True)


def legacy_upload_without_intake(
    setup: PortalSetup, *, client_upload: bool = True
) -> tuple[DocumentIntake, EvidenceLink]:
    """Model an older client upload without deleting any retained intake."""
    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            uploaded_by=setup.portal_user,
            sha256=hashlib.sha256(b"legacy unscanned upload").hexdigest(),
            original_filename="unsafe.xml",
            byte_size=23,
            content_type="application/xml",
            client_upload=client_upload,
        )
        link = EvidenceLink.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            request=setup.request,
            document=document,
            origin=LinkOrigin.PORTAL,
            submitted_by=setup.portal_user,
        )
        return DocumentIntake(document=document), link


@pytest.mark.parametrize(
    "case",
    [
        (IntakeState.RECEIVED, ScanVerdict.NONE, False, False, "recebido"),
        (IntakeState.QUARANTINED, ScanVerdict.NONE, False, False, "quarentena"),
        (
            IntakeState.SCAN_UNAVAILABLE,
            ScanVerdict.UNAVAILABLE,
            False,
            False,
            "verificacao_indisponivel",
        ),
        (
            IntakeState.REJECTED_MALWARE,
            ScanVerdict.INFECTED,
            False,
            False,
            "rejeitado_malware",
        ),
        (
            IntakeState.REJECTED_TYPE,
            ScanVerdict.NONE,
            False,
            False,
            "rejeitado_tipo",
        ),
        (IntakeState.LINKED, ScanVerdict.CLEAN, True, False, "rejeitado"),
        (IntakeState.LINKED, ScanVerdict.INFECTED, False, False, "infectado"),
        (IntakeState.LINKED, ScanVerdict.NONE, False, False, "sem_verificacao"),
        (IntakeState.RECEIVED, ScanVerdict.NONE, False, True, "sem_verificacao"),
    ],
)
def test_unsafe_original_is_excluded_without_blocking_clean_original(
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, bool, bool, str],
) -> None:
    # Given a real portal upload alongside a scanned-clean original.
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    state, verdict, rejected, missing, code = case
    setup = portal_setup("export-excluded")
    clean, _ = reviewed(setup, "nfe_emitente_cliente.xml")
    if missing:
        unsafe, link = legacy_upload_without_intake(setup)
    else:
        unsafe, link = reviewed(
            setup, "unsafe.xml", fixture("nfe_emitente_cliente.xml") + b"\n"
        )
    with tenant_context(setup.graph.tenant.pk):
        DocumentIntake.objects.filter(pk=unsafe.pk).update(state=state, verdict=verdict)
        if rejected:
            EvidenceLink.objects.filter(pk=link.pk).update(
                review_decision=ReviewDecision.REJECTED
            )
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
    opened: list[str] = []
    open_file = default_storage.open

    def observe_open(name: str, mode: str = "rb") -> File[bytes]:
        opened.append(name)
        return open_file(name, mode)

    monkeypatch.setattr(default_storage, "open", observe_open)

    # When the real phased worker builds the archive.
    build_export(str(setup.graph.tenant.pk), str(export.pk))

    # Then only the safe original is read and both output surfaces name the exclusion.
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY, export.error_code
        assert opened == [clean.document.storage_key]
        exclusions = export.excluded_originals
        assert [(row["id"], row["code"]) for row in exclusions] == [
            (str(unsafe.document_id), code)
        ]
        assert exclusions[0]["filename"] == unsafe.document.original_filename
        assert exclusions[0]["reason"]
        key, index = export.storage_key, export.index.data
    with default_storage.open(key) as handle, zipfile.ZipFile(handle) as archive:
        assert f"originals/{unsafe.document_id}.xml" not in archive.namelist()
        assert archive.read(f"originals/{clean.document_id}.xml")
        assert json.loads(archive.read("index.json")) == index
        assert json.loads(archive.read("excluded-originals.json")) == exclusions
        html = archive.read("index.html").decode()
        assert exclusions[0]["filename"] in html
        assert exclusions[0]["reason"] in html
        for line in archive.read("manifest.sha256").decode().splitlines():
            digest, member = line.split("  ", 1)
            assert digest == hashlib.sha256(archive.read(member)).hexdigest()
    response = setup.firm().get(reverse("evidence-export-detail", args=[export.pk]))
    assert response.status_code == 200
    assert response.context["export"].excluded_originals == exclusions
    assert exclusions[0]["filename"].encode() in response.content
    assert exclusions[0]["reason"].encode() in response.content
    # A later intake change must not rewrite what this ZIP actually excluded.
    with tenant_context(setup.graph.tenant.pk):
        DocumentIntake.objects.filter(pk=unsafe.pk).update(
            state=IntakeState.LINKED, verdict=ScanVerdict.CLEAN
        )
    refreshed = setup.firm().get(reverse("evidence-export-detail", args=[export.pk]))
    assert refreshed.context["export"].excluded_originals == exclusions


def test_staff_original_without_scan_is_also_excluded(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    # Given a staff original without a completed scan, never a safety exemption.
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = portal_setup("export-unscanned-staff")
    intake, _ = legacy_upload_without_intake(setup, client_upload=False)
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )

    # When exporting its cycle.
    build_export(str(setup.graph.tenant.pk), str(export.pk))

    # Then the archive names the missing scan and excludes all original bytes.
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY, export.error_code
        assert export.excluded_originals[0]["id"] == str(intake.document_id)
        assert export.excluded_originals[0]["code"] == "sem_verificacao"
        key = export.storage_key
    with default_storage.open(key) as handle, zipfile.ZipFile(handle) as archive:
        assert not any(name.startswith("originals/") for name in archive.namelist())


def test_concluded_cycle_exports_with_scanning_disabled(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    # Given a concluded cycle containing an original that was never scanned.
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.CLAMAV_ENABLED = False
    settings.DELIVERY_PAUSED = False
    setup = portal_setup("export-unscanned-completed")
    intake = submit(setup, "never-scanned.xml", b"<document>unscanned</document>")
    with FakeSMTP(mode="accept") as peer, smtp_at(peer.port):
        dispatch_tenant(str(setup.graph.tenant.pk))
    with tenant_context(setup.graph.tenant.pk):
        setup.request.required = False
        setup.request.save(update_fields=["required"])
        ClientAssignment.objects.create(
            tenant=setup.graph.tenant, client=setup.graph.client, user=setup.graph.staff
        )
        CycleTask.objects.filter(cycle=setup.cycle).update(state=TaskState.ACCEPTED)
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(
            state=CycleState.EXECUTION
        )
        assert not [
            item
            for item in completion_items(ServiceCycle.objects.get(pk=setup.cycle.pk))
            if item.code != "evidence_index"
        ]
        close_cycle(CompletionSetup(setup.graph, setup.cycle))
        setup.cycle.refresh_from_db()
        assert setup.cycle.state == CycleState.COMPLETED
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )

    # When exporting the preserved completion generation.
    build_export(str(setup.graph.tenant.pk), str(export.pk))

    # Then the complete index survives, but no original bytes leave quarantine.
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY, export.error_code
        key = export.storage_key
    with default_storage.open(key) as handle, zipfile.ZipFile(handle) as archive:
        assert not any(name.startswith("originals/") for name in archive.namelist())
        assert json.loads(archive.read("excluded-originals.json"))[0]["id"] == str(
            intake.document_id
        )


@pytest.mark.parametrize(
    "state",
    [
        IntakeState.VERIFIED,
        IntakeState.CLASSIFIED,
        IntakeState.DIVERGENT,
        IntakeState.LINKED,
    ],
)
def test_scanned_clean_export_keeps_existing_members(
    settings: SettingsWrapper, tmp_path: Path, state: str
) -> None:
    # Given an original in any existing scanned-clean state.
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    setup = portal_setup("export-clean-unchanged")
    intake, _ = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        DocumentIntake.objects.filter(pk=intake.pk).update(state=state)
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )

    # When the archive is built.
    build_export(str(setup.graph.tenant.pk), str(export.pk))

    # Then the old layout and source bytes remain unchanged.
    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY
        assert export.excluded_originals == []
        key = export.storage_key
    with default_storage.open(key) as handle, zipfile.ZipFile(handle) as archive:
        assert set(archive.namelist()) == {
            "index.json",
            "index.html",
            "manifest.sha256",
            f"originals/{intake.document_id}.xml",
        }
        assert (
            hashlib.sha256(
                archive.read(f"originals/{intake.document_id}.xml")
            ).hexdigest()
            == intake.document.sha256
        )

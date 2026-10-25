"""Actor-less provider ingestion shares the ordinary quarantine and scan sweep."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.retention import RetentionClass
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, EvidenceLink, IntakeState
from apps.cycles.intake import (
    UploadMissing,
    UploadRejected,
    UploadScannerOff,
    UploadTooLarge,
)
from apps.cycles.scanning import EICAR, FakeScanner
from apps.cycles.tasks import scan_quarantine
from apps.obligations.models import Document
from apps.tenants.models import Tenant
from tests.obligations.ingest_fixtures import PDF, ingest_setup, portal_cursor
from tests.portal.request_fixtures import PortalSetup, upload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    return ingest_setup(settings, tmp_path)


def _ingest(
    setup: PortalSetup,
    *,
    origin: str = "provedor_federal",
    receipt_ref: str = "sitfis-call-001",
    payload: bytes = PDF,
) -> Document:
    from apps.obligations.system_ingest import ingest_system_document  # noqa: PLC0415

    return ingest_system_document(
        tenant_id=setup.graph.tenant.pk,
        client_id=setup.graph.client.pk,
        origin=origin,
        receipt_ref=receipt_ref,
        payload=payload,
        filename="sitfis.pdf",
        content_type="text/html",
        retention_class=RetentionClass.FISCAL_EVIDENCE,
    )


@pytest.mark.parametrize("origin", ["provedor_federal", "captura_provedor"])
def test_provider_original_has_no_actor_and_stays_hidden_after_scan_sweep(
    setup: PortalSetup, origin: str
) -> None:
    # Given a provider original, never attributed to a fabricated human.
    document = _ingest(setup, origin=origin)
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get(document=document)
        assert (
            document.uploaded_by_id,
            document.client_upload,
            document.portal_visible,
        ) == (None, False, False)
        assert document.acquisition_origin == origin
        assert document.acquisition_receipt_ref == "sitfis-call-001"
        assert document.content_type == "application/pdf"
        assert intake.state == IntakeState.QUARANTINED
        assert default_storage.exists(intake.quarantine_key)
        assert not default_storage.exists(document.storage_key)
        assert not EvidenceLink.objects.filter(document=document).exists()
    # When the existing platform sweep scans it through the scanner port.
    scanner = FakeScanner()
    with patch("apps.cycles.tasks.scanner_from_settings", return_value=scanner):
        assert scan_quarantine.apply().get() == 1
    # Then it is clean and usable by staff, but still invisible to the real portal role.
    assert scanner.scanned == [len(PDF)]
    with tenant_context(setup.graph.tenant.pk):
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.VERIFIED
    with portal_cursor(setup) as cursor:
        cursor.execute(
            "SELECT id FROM obligations_document WHERE id = %s", [document.pk]
        )
        assert cursor.fetchall() == []
    assert setup.portal().get(f"/documentos/{document.storage_key}").status_code == 404


def test_ingest_is_idempotent_per_client_hash_and_invisible_partition(
    setup: PortalSetup,
) -> None:
    # Given visible bytes already uploaded by this client.
    assert (
        setup.portal()
        .post("/documentos/enviar", {"file": upload("client.pdf", PDF)})
        .status_code
        == 302
    )
    # When the same bytes arrive twice from the provider.
    first = _ingest(setup)
    again = _ingest(setup, receipt_ref="another-call")
    # Then one hidden original and one visible client upload retain their provenance.
    assert again.pk == first.pk
    assert again.acquisition_receipt_ref == "sitfis-call-001"
    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.filter(sha256=first.sha256).count() == 2
        assert DocumentIntake.objects.filter(document=first).count() == 1


@pytest.mark.parametrize("origin", ["upload_portal", "upload_equipe", "", "unknown"])
def test_system_origin_guard_refuses_before_any_row_or_bytes(
    setup: PortalSetup, origin: str
) -> None:
    from apps.obligations.system_ingest import InvalidSystemDocument  # noqa: PLC0415

    with pytest.raises(InvalidSystemDocument):
        _ingest(setup, origin=origin)
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()
    assert default_storage.listdir("") == ([], [])


def test_provider_receipt_is_required_before_storage(setup: PortalSetup) -> None:
    from apps.obligations.system_ingest import InvalidSystemDocument  # noqa: PLC0415

    with pytest.raises(InvalidSystemDocument):
        _ingest(setup, receipt_ref="")
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()


@pytest.mark.parametrize(
    ("payload", "error"),
    [(b"", UploadMissing), (b"<html><script>x</script>", UploadRejected)],
)
def test_shared_intake_limits_refuse_invalid_bytes(
    setup: PortalSetup, payload: bytes, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _ingest(setup, payload=payload)
    with tenant_context(setup.graph.tenant.pk):
        if payload:
            intake = DocumentIntake.objects.select_related("document").get()
            assert intake.state == IntakeState.REJECTED_TYPE
            assert intake.document.portal_visible is False
            assert default_storage.exists(intake.quarantine_key)
        else:
            assert not Document.objects.exists()


def test_system_ingest_enforces_the_shared_size_cap(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.PORTAL_UPLOAD_MAX_BYTES = len(PDF) - 1
    with pytest.raises(UploadTooLarge):
        _ingest(setup)
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()


def test_required_scanner_guard_applies_to_actorless_ingest(setup: PortalSetup) -> None:
    # Given required scanning with the configured scanner switched off.
    tenant = setup.graph.tenant
    tenant.features = ["cycles_r1", "scan_required"]
    tenant.save(update_fields=["features"])
    # When provider bytes arrive.
    with pytest.raises(UploadScannerOff):
        _ingest(setup)
    # Then the same pre-storage refusal as client intake holds.
    with tenant_context(tenant.pk):
        assert not Document.objects.exists()


def test_same_bytes_are_not_reused_across_clients_or_tenants(
    setup: PortalSetup,
) -> None:
    from apps.obligations.system_ingest import ingest_system_document  # noqa: PLC0415

    # Given the same payload already held for another client and another tenant.
    other_tenant = Tenant.objects.create(name="Other firm", slug="other-firm")
    with tenant_context(other_tenant.pk):
        other_client = ClientCompany.objects.create(
            tenant=other_tenant, legal_name="Other client", cnpj="11222333000181"
        )
    ids = []
    for tenant_id, client_id in (
        (other_tenant.pk, other_client.pk),
        (setup.graph.tenant.pk, setup.other_client.pk),
        (setup.graph.tenant.pk, setup.graph.client.pk),
    ):
        # When each trusted worker context ingests those exact bytes.
        document = ingest_system_document(
            tenant_id=tenant_id,
            client_id=client_id,
            origin="provedor_federal",
            receipt_ref="call-per-client",
            payload=PDF,
            filename="report.pdf",
            content_type="application/pdf",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
        )
        # Then every result belongs to the requested tenant/client.
        assert (document.tenant_id, document.client_id) == (tenant_id, client_id)
        ids.append(document.pk)
    assert len(set(ids)) == 3


def test_mismatched_tenant_client_is_refused_without_storage(
    setup: PortalSetup,
) -> None:
    from apps.obligations.system_ingest import ingest_system_document  # noqa: PLC0415

    # Given a tenant ID that does not own the supplied client.
    other = Tenant.objects.create(name="Wrong firm", slug="wrong-firm")
    # When a worker supplies the mismatched pair.
    with pytest.raises(ClientCompany.DoesNotExist):
        ingest_system_document(
            tenant_id=other.pk,
            client_id=setup.graph.client.pk,
            origin="provedor_federal",
            receipt_ref="wrong-pair",
            payload=PDF,
            filename="report.pdf",
            content_type="application/pdf",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
        )
    # Then neither metadata nor object bytes have been written.
    with tenant_context(other.pk):
        assert not Document.objects.exists()
    assert default_storage.listdir("") == ([], [])


def test_system_malware_stays_quarantined_and_never_becomes_portal_visible(
    setup: PortalSetup,
) -> None:
    document = _ingest(setup, payload=PDF + EICAR)
    with patch("apps.cycles.tasks.scanner_from_settings", return_value=FakeScanner()):
        assert scan_quarantine.apply().get() == 1
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get(document=document)
        assert intake.state == IntakeState.REJECTED_MALWARE
        assert default_storage.exists(intake.quarantine_key)
        assert not default_storage.exists(document.storage_key)
    with portal_cursor(setup) as cursor:
        cursor.execute("SELECT id FROM obligations_document")
        assert cursor.fetchall() == []


@pytest.mark.parametrize("field", ["uploaded_by", "acquisition_receipt_ref"])
def test_database_checks_refuse_missing_provider_receipt_or_human_upload_actor(
    setup: PortalSetup, field: str
) -> None:
    document = _ingest(setup)
    changes = (
        {"acquisition_origin": "upload_equipe"}
        if field == "uploaded_by"
        else {"acquisition_receipt_ref": ""}
    )
    with (
        tenant_context(setup.graph.tenant.pk),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        Document.objects.filter(pk=document.pk).update(**changes)

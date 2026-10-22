"""AXRM-005: `scan_required` makes the integrated scanner a precondition of intake.

Real portal and firm requests through the middleware (role switch, RLS); the scanner
port double is `FakeScanner` where a verdict is needed. With the flag on and
`CLAMAV_ENABLED=False`, an upload is refused before a byte is stored, and a file the
scanner never cleared is refused to staff with no POST bypass. Without the flag every
path below is the R1 behaviour, warning-download included.
"""

import hashlib
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import IntakeState, LinkOrigin
from apps.cycles.export_records import ExportState
from apps.cycles.export_service import request_export
from apps.cycles.export_tasks import build_export
from apps.cycles.models import CycleEvent, DocumentIntake, EvidenceLink
from apps.cycles.scanning import FakeScanner, scan_intake, scan_required
from apps.cycles.tasks import scan_quarantine
from apps.obligations.models import Document
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

pytestmark = pytest.mark.django_db(transaction=True)

FLAG = "scan_required"
PDF_BYTES = b"%PDF-1.4\n% axrm-005 sintetico\n%%EOF\n"
SCANNER_OFF_COPY = (
    "Envio temporariamente indisponível: a verificação antivírus está desligada"
)
UNSCANNED_REFUSAL = "não passou pela verificação antivírus"
WARNING_COPY = "não passou por verificação antivírus"


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    settings.ALLOWED_HOSTS = [".localhost", "localhost", "testserver"]
    settings.CLAMAV_ENABLED = False
    return portal_setup("axrm005-scan")


def _flag(setup: PortalSetup, *, on: bool) -> None:
    tenant = setup.graph.tenant
    features = [flag for flag in tenant.features if flag != FLAG]
    tenant.features = [*features, FLAG] if on else features
    tenant.save(update_fields=["features"])


def _request_post(setup: PortalSetup, name: str = "nota.pdf") -> Any:  # noqa: ANN401
    return setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload(name, PDF_BYTES)},
    )


def _rows(setup: PortalSetup) -> tuple[int, int, int, int]:
    with tenant_context(setup.graph.tenant.pk):
        return (
            Document.objects.count(),
            DocumentIntake.objects.count(),
            EvidenceLink.objects.count(),
            CycleEvent.objects.filter(type="evidence.submitted").count(),
        )


def _stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def _unavailable(setup: PortalSetup) -> DocumentIntake:
    """An R1 upload the disabled scanner sweep left `verificacao_indisponivel`."""
    assert _request_post(setup).status_code == HTTPStatus.FOUND
    assert scan_quarantine.apply().get() == 1
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.select_related("document").get()
    assert intake.state == IntakeState.SCAN_UNAVAILABLE
    return intake


def _never_scanned(setup: PortalSetup) -> Document:
    """A client upload that predates intakes: bytes at the final key, no intake."""
    payload = b"%PDF-1.4\n% legado sem verificacao\n%%EOF\n"
    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            uploaded_by=setup.portal_user,
            sha256=hashlib.sha256(payload).hexdigest(),
            original_filename="legado.pdf",
            byte_size=len(payload),
            content_type="application/pdf",
        )
        default_storage.save(document.storage_key, upload("legado.pdf", payload))
        EvidenceLink.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            request=setup.request,
            document=document,
            origin=LinkOrigin.PORTAL,
            submitted_by=setup.portal_user,
        )
    return document


def _unscanned_events(setup: PortalSetup) -> int:
    with tenant_context(setup.graph.tenant.pk):
        return CycleEvent.objects.filter(type="document.unscanned_downloaded").count()


# --- the flag ---------------------------------------------------------------------


def test_the_flag_binds_only_with_cycles_r1_and_obeys_the_kill_switch(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    tenant = setup.graph.tenant
    assert scan_required(tenant.pk) is False
    _flag(setup, on=True)
    assert scan_required(tenant.pk) is True
    tenant.features = [FLAG]
    tenant.save(update_fields=["features"])
    assert scan_required(tenant.pk) is False, "scan_required requires cycles_r1"
    tenant.features = ["cycles_r1", FLAG]
    tenant.save(update_fields=["features"])
    settings.AXORIM_DISABLED_FEATURES = [FLAG]
    assert scan_required(tenant.pk) is False


# --- intake refusal ---------------------------------------------------------------


def test_request_upload_is_refused_with_the_scanner_off_and_nothing_is_stored(
    setup: PortalSetup, tmp_path: Path
) -> None:
    _flag(setup, on=True)

    response = _request_post(setup)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert SCANNER_OFF_COPY in response.content.decode()
    assert _rows(setup) == (0, 0, 0, 0)
    assert _stored_files(tmp_path) == []


def test_vault_upload_is_refused_with_the_scanner_off_and_nothing_is_stored(
    setup: PortalSetup, tmp_path: Path
) -> None:
    _flag(setup, on=True)

    response = setup.portal().post(
        "/documentos/enviar", {"file": upload("cofre.pdf", PDF_BYTES)}
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.context["reason"] == "scanner_off"
    assert SCANNER_OFF_COPY in response.content.decode()
    assert _rows(setup) == (0, 0, 0, 0)
    assert _stored_files(tmp_path) == []


def test_with_the_scanner_on_a_flagged_upload_is_accepted_into_quarantine(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flag(setup, on=True)
    settings.CLAMAV_ENABLED = True

    assert _request_post(setup).status_code == HTTPStatus.FOUND

    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get()
    assert intake.state == IntakeState.QUARANTINED
    assert _rows(setup) == (1, 1, 1, 1)


def test_flag_off_upload_with_the_scanner_off_keeps_the_r1_warning_download(
    setup: PortalSetup,
) -> None:
    intake = _unavailable(setup)
    firm = setup.firm()
    url = f"/documentos/{intake.document_id}/baixar/"

    warning = firm.get(url)
    assert warning.status_code == HTTPStatus.OK
    assert WARNING_COPY in warning.content.decode()
    assert 'name="confirmar" value="1"' in warning.content.decode()
    confirmed = firm.post(url, {"confirmar": "1"})
    assert confirmed.status_code == HTTPStatus.OK
    assert confirmed.content == PDF_BYTES
    assert _unscanned_events(setup) == 1


# --- staff download refusal -------------------------------------------------------


def test_staff_download_of_an_unavailable_file_is_refused_with_no_post_bypass(
    setup: PortalSetup,
) -> None:
    intake = _unavailable(setup)
    _flag(setup, on=True)
    firm = setup.firm()
    url = f"/documentos/{intake.document_id}/baixar/"

    for response in (firm.get(url), firm.post(url, {"confirmar": "1"})):
        assert response.status_code == HTTPStatus.FORBIDDEN
        assert PDF_BYTES not in response.content
        body = response.content.decode()
        assert UNSCANNED_REFUSAL in body
        assert 'name="confirmar"' not in body
    assert _unscanned_events(setup) == 0


def test_staff_download_of_a_never_scanned_file_is_refused_under_the_flag_only(
    setup: PortalSetup,
) -> None:
    document = _never_scanned(setup)
    firm = setup.firm()
    url = f"/documentos/{document.pk}/baixar/"
    assert firm.get(url).status_code == HTTPStatus.OK  # R1: the warning first

    _flag(setup, on=True)

    for response in (firm.get(url), firm.post(url, {"confirmar": "1"})):
        assert response.status_code == HTTPStatus.FORBIDDEN
        assert b"legado sem verificacao" not in response.content
    assert _unscanned_events(setup) == 0


def test_a_scanned_clean_file_is_still_served_under_the_flag(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    _flag(setup, on=True)
    settings.CLAMAV_ENABLED = True
    assert _request_post(setup).status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get()
    scan_intake(
        setup.graph.tenant.pk, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )

    served = setup.firm().get(f"/documentos/{intake.document_id}/baixar/")

    assert served.status_code == HTTPStatus.OK
    assert served.content == PDF_BYTES


def test_an_unassigned_staff_accountant_still_gets_404_and_writes_nothing(
    setup: PortalSetup,
) -> None:
    intake = _unavailable(setup)
    _flag(setup, on=True)
    staff = setup.firm(setup.graph.staff)
    url = f"/documentos/{intake.document_id}/baixar/"

    assert staff.get(url).status_code == HTTPStatus.NOT_FOUND
    assert staff.post(url, {"confirmar": "1"}).status_code == HTTPStatus.NOT_FOUND
    assert _unscanned_events(setup) == 0


# --- exports ----------------------------------------------------------------------


def test_exports_keep_excluding_unscanned_originals_under_the_flag(
    setup: PortalSetup,
) -> None:
    clean, _ = reviewed(setup, "nfe_emitente_cliente.xml")
    unscanned = _never_scanned(setup)
    _flag(setup, on=True)
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        export = request_export(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )

    build_export(str(setup.graph.tenant.pk), str(export.pk))

    with tenant_context(setup.graph.tenant.pk):
        export.refresh_from_db()
        assert export.state == ExportState.READY, export.error_code
        assert [(row["id"], row["code"]) for row in export.excluded_originals] == [
            (str(unscanned.pk), "sem_verificacao")
        ]
        assert str(clean.document_id) not in {
            row["id"] for row in export.excluded_originals
        }


# --- per-file state (T70) ---------------------------------------------------------


def test_cycle_detail_names_each_unscanned_file_state_and_offers_no_risk_download(
    setup: PortalSetup,
) -> None:
    intake = _unavailable(setup)
    document = _never_scanned(setup)
    url = reverse("cycle-detail", args=[setup.cycle.pk])
    r1 = setup.firm().get(url).content.decode()
    assert "Baixar com aviso" in r1

    _flag(setup, on=True)
    page = setup.firm().get(url).content.decode()

    assert "Baixar com aviso" not in page
    assert "verificação antivírus indisponível: arquivo retido, não entregue" in page
    assert "sem verificação antivírus: arquivo retido, não entregue" in page
    for document_id in (intake.document_id, document.pk):
        download = reverse("staff-document-download", args=[document_id])
        assert f'href="{download}"' not in page


def test_document_review_shows_the_intake_state_and_no_risk_download(
    setup: PortalSetup,
) -> None:
    intake = _unavailable(setup)
    with tenant_context(setup.graph.tenant.pk):
        link = EvidenceLink.objects.get(document_id=intake.document_id)
    url = reverse("document-review", args=[link.pk])
    assert "Baixar com aviso de risco" in setup.firm().get(url).content.decode()

    _flag(setup, on=True)
    page = setup.firm().get(url).content.decode()

    assert "Baixar com aviso de risco" not in page
    assert 'data-intake-state="verificacao_indisponivel"' in page
    assert "Situação do arquivo: Verificação indisponível" in page

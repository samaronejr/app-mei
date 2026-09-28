"""Upload against a request (S7.5): link created, cap, foreign id, attribution."""

from http import HTTPStatus
from pathlib import Path

import pytest
from django.core.files.storage import default_storage
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.models import (
    CycleEvent,
    DocumentIntake,
    EvidenceLink,
    IntakeState,
    LinkOrigin,
)
from apps.obligations.models import Document
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

pytestmark = pytest.mark.django_db(transaction=True)

PDF = b"%PDF-1.4 synthetic nota de servico setembro\n"
TOO_LARGE_MESSAGE = "O arquivo passa do limite de"


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("upload-req")


def _counts(setup: PortalSetup) -> tuple[int, int, int, int]:
    with tenant_context(setup.graph.tenant.pk):
        return (
            Document.objects.count(),
            EvidenceLink.objects.count(),
            DocumentIntake.objects.count(),
            CycleEvent.objects.filter(type="evidence.submitted").count(),
        )


def test_upload_creates_document_intake_link_and_event(setup: PortalSetup) -> None:
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("nota.pdf", PDF)},
    )
    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == f"/solicitacoes/{setup.request.pk}/"
    with tenant_context(setup.graph.tenant.pk):
        link = EvidenceLink.objects.get()
        document = link.document
        intake = DocumentIntake.objects.get()
        event = CycleEvent.objects.get(type="evidence.submitted")
    assert link.request_id == setup.request.pk
    assert link.client_id == setup.graph.client.pk
    assert link.origin == LinkOrigin.PORTAL
    assert link.relinked is False
    assert link.submitted_by_id == setup.portal_user.pk
    assert document.client_id == setup.graph.client.pk
    assert document.uploaded_by_id == setup.portal_user.pk
    assert document.byte_size == len(PDF)
    assert intake.document_id == document.pk
    assert intake.state == IntakeState.RECEIVED
    assert event.client_id == setup.graph.client.pk
    assert event.actor_id == setup.portal_user.pk
    assert event.payload["link_id"] == str(link.pk)
    # Ids and hashes only; never the filename or bytes.
    assert "nota.pdf" not in str(event.payload)
    with default_storage.open(document.storage_key) as handle:
        assert handle.read() == PDF
    page = setup.portal().get(f"/solicitacoes/{setup.request.pk}/")
    body = page.content.decode()
    assert 'data-status="recebido">Recebido<' in body
    assert "nota.pdf" in body


def test_htmx_upload_returns_the_status_partial_with_the_recebido_chip(
    setup: PortalSetup,
) -> None:
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("nota.pdf", PDF)},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert body.lstrip().startswith("<section") or 'id="request-status"' in body
    assert 'id="request-status"' in body
    assert 'data-status="recebido">Recebido<' in body
    assert "<html" not in body
    assert "badge-ok" not in body


def test_over_the_cap_is_400_in_pt_br_and_writes_nothing(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.PORTAL_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    before = _counts(setup)
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("grande.pdf", oversized)},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    body = response.content.decode()
    assert TOO_LARGE_MESSAGE in body
    assert "10,0 MB" in body
    assert _counts(setup) == before
    htmx = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("grande.pdf", oversized)},
        headers={"HX-Request": "true"},
    )
    assert htmx.status_code == HTTPStatus.BAD_REQUEST
    assert TOO_LARGE_MESSAGE in htmx.content.decode()
    assert _counts(setup) == before


def test_exactly_at_the_cap_is_accepted(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.PORTAL_UPLOAD_MAX_BYTES = 2048
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("limite.pdf", b"y" * 2048)},
    )
    assert response.status_code == HTTPStatus.FOUND


def test_foreign_request_id_is_404_and_writes_nothing(setup: PortalSetup) -> None:
    before = _counts(setup)
    response = setup.portal().post(
        f"/solicitacoes/{setup.other_request.pk}/enviar",
        {"file": upload("nota.pdf", PDF)},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert _counts(setup) == before


def test_attribution_ignores_client_and_tenant_in_the_post_body(
    setup: PortalSetup,
) -> None:
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {
            "file": upload("nota.pdf", PDF),
            "client": str(setup.other_client.pk),
            "client_id": str(setup.other_client.pk),
            "tenant": "00000000-0000-0000-0000-000000000000",
            "submitted_by": str(setup.graph.actor.pk),
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        link = EvidenceLink.objects.get()
    assert link.client_id == setup.graph.client.pk
    assert link.submitted_by_id == setup.portal_user.pk


def test_missing_file_is_400_and_a_waived_request_is_404(setup: PortalSetup) -> None:
    missing = setup.portal().post(f"/solicitacoes/{setup.request.pk}/enviar", {})
    assert missing.status_code == HTTPStatus.BAD_REQUEST
    assert "Escolha um arquivo" in missing.content.decode()
    from apps.cycles.services.requests import waive_request  # noqa: PLC0415

    with tenant_context(setup.graph.tenant.pk):
        waive_request(
            setup.graph.actor,
            setup.request.pk,
            reason="não se aplica",
            expected_version=setup.request.version,
        )
    closed = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("nota.pdf", PDF)},
    )
    assert closed.status_code == HTTPStatus.NOT_FOUND


def test_interim_new_upload_is_served_only_to_its_uploader(
    setup: PortalSetup,
) -> None:
    from apps.tenants.models import TenantRole  # noqa: PLC0415
    from tests.ui.factories import add_member  # noqa: PLC0415

    setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("nota.pdf", PDF)},
    )
    with tenant_context(setup.graph.tenant.pk):
        key = EvidenceLink.objects.get().document.storage_key
    colleague = add_member(
        setup.graph.tenant,
        "colega@upload-req.test",
        TenantRole.CLIENT_COLLABORATOR,
        client=setup.graph.client,
    )
    own = setup.portal()
    assert own.get(f"/documentos/{key}").status_code == HTTPStatus.OK
    assert "nota.pdf" in own.get("/documentos/").content.decode()
    other = setup.portal(colleague)
    assert other.get(f"/documentos/{key}").status_code == HTTPStatus.NOT_FOUND
    assert "nota.pdf" not in other.get("/documentos/").content.decode()

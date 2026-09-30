"""S15 B: a client uploads another company's document (T13).

Given a PDF or XML whose CNPJ is not the cycle client's, then the intake is
`divergente`, the link does not count as sent, a `documento_de_outra_empresa`
exception opens on the cycle, and the document reaches no other client -- not even
the firm's other client whose CNPJ it carries. It is never reassigned. "Not
searchable" (T21) and "not shadow-queued" (T24) are asserted by those todos.
"""

from pathlib import Path

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    ExtractedField,
    IntakeState,
    counted_links,
)
from apps.cycles.extraction import DIVERGENT, UNVERIFIABLE
from apps.cycles.extraction.service import EXCEPTION_REASON, extract_intake
from apps.cycles.models import Exception as CycleException
from apps.obligations.models import Document
from apps.tenants.models import TenantRole
from tests.cycles.document_fixtures import OTHER_CLIENT_CNPJ, reviewed
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.ui.factories import add_member

DB = pytest.mark.django_db(transaction=True)
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("t13-wrong-company")


def _exceptions(setup: PortalSetup) -> list[CycleException]:
    with tenant_context(setup.graph.tenant.pk):
        return list(CycleException.objects.filter(reason_code=EXCEPTION_REASON))


@DB
@pytest.mark.parametrize("name", ["nfe_outra_empresa.xml", "recibo_outra_empresa.pdf"])
def test_other_company_document_is_divergent_suspended_and_raises_an_exception(
    setup: PortalSetup, name: str
) -> None:
    intake, link = reviewed(setup, name)

    assert intake.state == IntakeState.DIVERGENT
    assert intake.reason == DIVERGENT
    with tenant_context(setup.graph.tenant.pk):
        # Not linked: the link exists (the record of what was sent) but is suspended.
        assert EvidenceLink.objects.filter(pk=link.pk).exists()
        assert not counted_links().filter(pk=link.pk).exists()
        document = Document.objects.get(pk=intake.document_id)
        # Never reassigned: the document, its intake and its fields stay with the
        # client that sent it, and nothing was written under the other client.
        assert document.client_id == setup.graph.client.pk
        assert not Document.objects.filter(client=setup.other_client).exists()
        assert not EvidenceLink.objects.filter(client=setup.other_client).exists()
        assert not DocumentIntake.objects.filter(client=setup.other_client).exists()
        assert not ExtractedField.objects.filter(client=setup.other_client).exists()
        assert OTHER_CLIENT_CNPJ in set(
            ExtractedField.objects.filter(document=document).values_list(
                "value", flat=True
            )
        )
    (problem,) = _exceptions(setup)
    assert problem.cycle_id == setup.cycle.pk
    assert problem.subject_ref == intake.pk
    assert problem.evidence_refs == [str(intake.document_id)]
    assert problem.state == "aberta"
    assert problem.blocks_completion is True
    assert problem.client_visible is False

    # The uploader's portal no longer counts it as sent, nor serves it.
    portal = setup.portal()
    page = portal.get(f"/solicitacoes/{setup.request.pk}/").content.decode()
    assert 'data-status="recebido"' not in page
    assert portal.get(f"/documentos/{document.storage_key}").status_code == 404
    assert name not in portal.get("/documentos/").content.decode()

    # The firm's other client -- whose CNPJ the document carries -- sees nothing.
    stranger = add_member(
        setup.graph.tenant,
        "dono@outra-empresa.test",
        TenantRole.CLIENT_OWNER,
        client=setup.other_client,
    )
    other_portal = setup.portal(stranger)
    assert name not in other_portal.get("/documentos/").content.decode()
    assert other_portal.get(f"/documentos/{document.storage_key}").status_code == 404
    assert other_portal.get(f"/solicitacoes/{setup.request.pk}/").status_code == 404

    # The firm sees it in the divergent queue.
    queue = setup.firm().get("/documentos/?fila=divergentes").content.decode()
    assert f'data-link-id="{link.pk}"' in queue
    assert 'data-intake-state="divergente"' in queue


@DB
@pytest.mark.parametrize(
    "name", ["nfe_emitente_cliente.xml", "nfse_nacional.xml", "recibo_cliente.pdf"]
)
def test_client_document_with_a_counterpart_is_not_divergent(
    setup: PortalSetup, name: str
) -> None:
    intake, link = reviewed(setup, name)

    assert intake.state == IntakeState.CLASSIFIED
    assert _exceptions(setup) == []
    with tenant_context(setup.graph.tenant.pk):
        assert counted_links().filter(pk=link.pk).exists()


@DB
def test_image_is_not_verifiable_and_never_divergent(setup: PortalSetup) -> None:
    intake, _link = reviewed(setup, "foto-nota.png", PNG_BYTES)

    assert intake.state == IntakeState.CLASSIFIED
    assert intake.reason == UNVERIFIABLE
    assert _exceptions(setup) == []
    page = setup.firm().get("/documentos/").content.decode()
    assert "foto-nota.png" in page


@DB
def test_repeat_extraction_does_not_open_a_second_exception(
    setup: PortalSetup,
) -> None:
    intake, _link = reviewed(setup, "nfe_outra_empresa.xml")

    extract_intake(setup.graph.tenant.pk, intake.pk, now=timezone.now())

    assert len(_exceptions(setup)) == 1

"""T13 review decisions: `version` concurrency (409), scope, provenance, portal reason.

Two real sessions POST the review page drawn at the same request revision; the loser
gets 409 and writes nothing. An unassigned staff accountant is 404 with no row
written. A rejection's pt-BR reason reaches the client in the portal; the firm-only
`Review` never does.
"""

from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from django.core.files.storage import default_storage
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.errors import VersionConflict
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExtractedField,
    FieldOrigin,
    IntakeState,
    RequestState,
    Review,
    ReviewDecision,
)
from apps.cycles.models import CycleEvent
from apps.cycles.services.review import record_review
from apps.obligations.models import Document
from tests.cycles.document_fixtures import (
    COUNTERPART_CNPJ,
    fixture,
    reviewed,
    scan_clean,
    submit,
)
from tests.portal.request_fixtures import PortalSetup, portal_setup

DB = pytest.mark.django_db(transaction=True)
REASON = "O arquivo está ilegível. Envie a nota em PDF ou XML."


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("t13-review")


def _post(client: Client, link: EvidenceLink, **data: Any) -> Any:  # noqa: ANN401
    return client.post(f"/documentos/{link.pk}/revisar/", data)


def _request(setup: PortalSetup) -> EvidenceRequest:
    return setup.reload(setup.request)


def _counts(setup: PortalSetup) -> tuple[int, int, int]:
    with tenant_context(setup.graph.tenant.pk):
        return (
            Review.objects.count(),
            ExtractedField.objects.filter(origin=FieldOrigin.STAFF).count(),
            CycleEvent.objects.filter(type="document.reviewed").count(),
        )


@DB
def test_second_reviewer_on_a_stale_revision_gets_409_and_writes_nothing(
    setup: PortalSetup,
) -> None:
    _intake, link = reviewed(setup, "recibo_cliente.pdf")
    drawn_at = _request(setup).version
    first, second = setup.firm(), setup.firm()
    assert first.get(f"/documentos/{link.pk}/revisar/").status_code == HTTPStatus.OK
    assert second.get(f"/documentos/{link.pk}/revisar/").status_code == HTTPStatus.OK

    won = _post(
        first,
        link,
        decision="rejeitado",
        client_reason=REASON,
        expected_version=drawn_at,
    )
    lost = _post(
        second,
        link,
        decision="aceito",
        client_reason="",
        expected_version=drawn_at,
        campo_competencia="2026-10",
    )

    assert won.status_code == HTTPStatus.FOUND
    assert lost.status_code == HTTPStatus.CONFLICT
    assert "Este registro mudou" in lost.content.decode()
    assert _counts(setup) == (1, 0, 1)
    request = _request(setup)
    assert request.state == RequestState.REJECTED
    assert request.version == drawn_at + 1
    with tenant_context(setup.graph.tenant.pk):
        link.refresh_from_db()
        assert link.review_decision == ReviewDecision.REJECTED
        assert link.client_reason == REASON


@DB
def test_service_refuses_a_stale_version_before_any_write(setup: PortalSetup) -> None:
    _intake, link = reviewed(setup, "recibo_cliente.pdf")
    stale = _request(setup).version - 1

    with tenant_context(setup.graph.tenant.pk), pytest.raises(VersionConflict):
        record_review(
            setup.graph.actor,
            link.pk,
            decision=ReviewDecision.ACCEPTED,
            client_reason="",
            corrections=[],
            expected_version=stale,
        )

    assert _counts(setup) == (0, 0, 0)


@DB
def test_unassigned_staff_accountant_gets_404_and_writes_nothing(
    setup: PortalSetup,
) -> None:
    _intake, link = reviewed(setup, "recibo_cliente.pdf")
    staff = setup.firm(setup.graph.staff)
    version = _request(setup).version

    assert staff.get(f"/documentos/{link.pk}/revisar/").status_code == 404
    response = _post(
        staff, link, decision="aceito", client_reason="", expected_version=version
    )
    assert response.status_code == 404
    assert staff.get(f"/documentos/{link.pk}/visualizar/").status_code == 404
    queue = staff.get("/documentos/")
    assert queue.status_code == HTTPStatus.OK
    assert f'data-link-id="{link.pk}"' not in queue.content.decode()
    assert _counts(setup) == (0, 0, 0)
    assert _request(setup).version == version


@DB
def test_rejection_reason_is_shown_to_the_client_and_the_request_reopens(
    setup: PortalSetup,
) -> None:
    _intake, link = reviewed(setup, "recibo_cliente.pdf")
    portal = setup.portal()
    before = portal.get(f"/solicitacoes/{setup.request.pk}/").content.decode()
    assert 'data-status="recebido"' in before

    response = _post(
        setup.firm(),
        link,
        decision="rejeitado",
        client_reason=REASON,
        expected_version=_request(setup).version,
    )

    assert response.status_code == HTTPStatus.FOUND
    page = portal.get(f"/solicitacoes/{setup.request.pk}/").content.decode()
    assert REASON in page
    assert "Arquivos não aceitos pela equipe" in page
    assert 'data-status="rejeitado"' in page
    assert "Enviar novamente" in page
    # The client can send again (a rejected request is open to the client).
    assert 'name="file"' in page


@DB
def test_accept_links_the_intake_keeps_the_suggestion_and_never_touches_bytes(
    setup: PortalSetup,
) -> None:
    intake, link = reviewed(setup, "nfe_emitente_cliente.xml")
    payload = fixture("nfe_emitente_cliente.xml")

    response = _post(
        setup.firm(),
        link,
        decision="aceito",
        client_reason="",
        expected_version=_request(setup).version,
        campo_numero="124",
        campo_destinatario_cnpj="12.abc.345/01de-35",
    )

    assert response.status_code == HTTPStatus.FOUND
    assert _request(setup).state == RequestState.ACCEPTED
    with tenant_context(setup.graph.tenant.pk):
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.LINKED
        numbers = dict(
            ExtractedField.objects.filter(name="numero").values_list("origin", "value")
        )
        review = Review.objects.get()
        document = Document.objects.get(pk=intake.document_id)
    # The suggestion stays beside the correction; an equal (re-masked) CNPJ is no
    # correction at all.
    assert numbers == {FieldOrigin.XML: "123", FieldOrigin.STAFF: "124"}
    assert review.corrections_json == [
        {
            "field": "numero",
            "suggested_value": "123",
            "reviewed_value": "124",
            "suggested_origin": "Sugerido: XML da nota",
        }
    ]
    assert review.document_sha256 == document.sha256
    with default_storage.open(document.storage_key, "rb") as handle:
        assert handle.read() == payload
    assert COUNTERPART_CNPJ.encode() in payload


@DB
@pytest.mark.parametrize(
    ("data", "status", "message"),
    [
        ({"decision": "rejeitado", "client_reason": "  "}, 422, "Explique ao cliente"),
        ({"decision": "talvez"}, 422, "Escolha aceitar"),
        ({"decision": "aceito", "campo_cnpj_1": "11222333000182"}, 422, "inválido"),
        ({"decision": "aceito", "campo_inexistente": "x"}, 302, ""),
    ],
)
def test_invalid_decisions_are_refused_without_writes(
    setup: PortalSetup, data: dict[str, str], status: int, message: str
) -> None:
    _intake, link = reviewed(setup, "recibo_cliente.pdf")

    response = _post(
        setup.firm(), link, expected_version=_request(setup).version, **data
    )

    assert response.status_code == status
    assert message in response.content.decode()
    if status == 422:
        assert _counts(setup) == (0, 0, 0)


@DB
def test_review_waits_for_extraction(setup: PortalSetup) -> None:
    intake = submit(setup, "recibo_cliente.pdf")
    scan_clean(setup, intake)
    with tenant_context(setup.graph.tenant.pk):
        link = EvidenceLink.objects.get(document_id=intake.document_id)

    response = _post(
        setup.firm(),
        link,
        decision="aceito",
        client_reason="",
        expected_version=_request(setup).version,
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "leitura automática" in response.content.decode()
    assert _counts(setup) == (0, 0, 0)


@DB
def test_divergent_accept_requires_the_third_party_record(setup: PortalSetup) -> None:
    intake, link = reviewed(setup, "nfe_outra_empresa.xml")
    firm = setup.firm()

    refused = _post(
        firm,
        link,
        decision="aceito",
        client_reason="",
        expected_version=_request(setup).version,
    )
    assert refused.status_code == 422
    assert _counts(setup) == (0, 0, 0)

    accepted = _post(
        firm,
        link,
        decision="aceito",
        client_reason="",
        third_party="1",
        override_reason="Nota do fornecedor exigida pelo contrato.",
        expected_version=_request(setup).version,
    )

    assert accepted.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        review = Review.objects.get()
        assert DocumentIntake.objects.get(pk=intake.pk).state == IntakeState.LINKED
        assert Document.objects.get(pk=intake.document_id).client_id == (
            setup.graph.client.pk
        )
    assert review.third_party is True
    assert review.override_reason == "Nota do fornecedor exigida pelo contrato."


@DB
def test_a_decided_file_cannot_be_decided_again(setup: PortalSetup) -> None:
    _intake, link = reviewed(setup, "recibo_cliente.pdf")
    firm = setup.firm()
    _post(
        firm,
        link,
        decision="rejeitado",
        client_reason=REASON,
        expected_version=_request(setup).version,
    )

    again = _post(
        firm,
        link,
        decision="aceito",
        client_reason="",
        expected_version=_request(setup).version,
    )

    assert again.status_code == HTTPStatus.CONFLICT
    assert _counts(setup) == (1, 0, 1)

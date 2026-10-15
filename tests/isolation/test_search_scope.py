"""Search sees exactly the actor's book, never a neighbour's (S7.2, AXR1-021).

Every assertion here runs against real rows through the real scoping seam
(`visible_clients`), on both the service and the firm endpoint. Each negative
carries a positive control on the same data, so an empty answer proves the scope
held rather than that the search matched nothing at all.
"""

from pathlib import Path
from uuid import UUID

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientAssignment, ClientCompany
from apps.core.search import SearchHit, SearchKind, search
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import IntakeState
from apps.obligations.models import Document
from tests.cycles.document_fixtures import OTHER_CLIENT_CNPJ, reviewed
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.ui.factories import make_cnpj

DB = pytest.mark.django_db(transaction=True)
SHARED_CNPJ = "11222333000181"
DIVERGENT_NAME = "nfe_outra_empresa.xml"
CLEAN_NAME = "nfe_emitente_cliente.xml"


def _hits(setup: PortalSetup, q: str, actor: User | None = None) -> list[SearchHit]:
    user = actor or setup.graph.actor
    with tenant_context(setup.graph.tenant.pk):
        return search(user, q)


def _ids(hits: list[SearchHit], kind: SearchKind) -> set[UUID]:
    return {hit.id for hit in hits if hit.kind is kind}


def _assign(setup: PortalSetup, client_id: UUID) -> None:
    with tenant_context(setup.graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=setup.graph.tenant,
            client_id=client_id,
            user=setup.graph.staff,
            role="primary",
        )


def _body(setup: PortalSetup, q: str, actor: User | None = None) -> str:
    response = setup.firm(actor).get("/busca/", {"q": q}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200, response.status_code
    return response.content.decode()


@pytest.fixture
def media(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


@DB
def test_same_cnpj_in_two_firms_each_sees_only_its_own() -> None:
    first = portal_setup("t21-firma-a")
    second = portal_setup("t21-firma-b")
    assert first.graph.client.cnpj == second.graph.client.cnpj == SHARED_CNPJ

    for mine, theirs in ((first, second), (second, first)):
        found = _ids(_hits(mine, SHARED_CNPJ), SearchKind.CLIENT)
        assert found == {mine.graph.client.pk}
        body = _body(mine, "11.222.333/0001-81")
        assert str(mine.graph.client.pk) in body
        assert str(theirs.graph.client.pk) not in body


@DB
def test_an_unassigned_client_is_hidden_until_assigned() -> None:
    setup = portal_setup("t21-unassigned")
    staff = setup.graph.staff

    # Vacuity control: the owner's portfolio is the firm, and the same queries hit.
    owner_hits = _hits(setup, "Synthetic")
    assert _ids(owner_hits, SearchKind.CLIENT) == {setup.graph.client.pk}
    assert _ids(owner_hits, SearchKind.CYCLE) == {setup.cycle.pk}
    assert _ids(_hits(setup, "Notas de serviço"), SearchKind.REQUEST) == {
        setup.request.pk
    }

    # The staff accountant holds clients.view_all on the matrix, but an unassigned
    # client is outside their book: no client, cycle or request hit, and no trace of
    # it in the rendered fragment.
    assert _hits(setup, "Synthetic", staff) == []
    assert _hits(setup, "Notas de serviço", staff) == []
    assert _hits(setup, SHARED_CNPJ, staff) == []
    body = _body(setup, "Synthetic", staff)
    assert str(setup.graph.client.pk) not in body
    assert "Synthetic client" not in body

    _assign(setup, setup.graph.client.pk)
    assert _ids(_hits(setup, "Synthetic", staff), SearchKind.CLIENT) == {
        setup.graph.client.pk
    }
    # Assignment to one client never widens to its neighbour in the same firm.
    assert _hits(setup, "Other client", staff) == []
    assert _ids(_hits(setup, "Other client"), SearchKind.CLIENT) == {
        setup.other_client.pk
    }


@DB
def test_standalone_documents_are_scoped_and_their_destinations_open() -> None:
    first = portal_setup("t21-standalone-a")
    second = portal_setup("t21-standalone-b")
    filename = "Vaultunique Comprovante.pdf"
    documents = []
    for setup, client in (
        (first, first.graph.client),
        (first, first.other_client),
        (second, second.graph.client),
    ):
        with tenant_context(setup.graph.tenant.pk):
            documents.append(
                Document.objects.create(
                    tenant=setup.graph.tenant,
                    client=client,
                    uploaded_by=setup.graph.actor,
                    original_filename=filename,
                    content_type="application/pdf",
                    byte_size=1,
                    sha256="a" * 64,
                )
            )
    assigned, unassigned, foreign = documents
    _assign(first, first.graph.client.pk)

    # Identical filenames are positive controls in each owner's own firm; the
    # accountant can find only the assigned client's standalone document.
    for setup, actor, expected in (
        (first, first.graph.actor, {assigned.pk, unassigned.pk}),
        (first, first.graph.staff, {assigned.pk}),
        (second, second.graph.actor, {foreign.pk}),
    ):
        hits = [
            hit
            for hit in _hits(setup, filename, actor)
            if hit.kind is SearchKind.DOCUMENT
        ]
        assert {hit.id for hit in hits} == expected
        body = _body(setup, filename, actor)
        assert body.count('data-kind="document"') == len(expected)
        assert "data-total" not in body
        browser = setup.firm(actor)
        for hit in hits:
            assert hit.url == f"/documentos/{hit.id}/baixar/"
            response = browser.get(hit.url)
            assert response.status_code == 200
            assert filename in response.content.decode()
        for hidden in {document.pk for document in documents} - expected:
            assert str(hidden) not in body
            assert browser.get(f"/documentos/{hidden}/baixar/").status_code == 404


@DB
def test_a_divergent_documents_extracted_cnpj_never_surfaces_it(media: None) -> None:
    setup = portal_setup("t21-divergent")
    divergent, _ = reviewed(setup, DIVERGENT_NAME)
    clean, clean_link = reviewed(setup, CLEAN_NAME)
    assert divergent.state == IntakeState.DIVERGENT
    assert clean.state == IntakeState.CLASSIFIED
    _assign(setup, setup.graph.client.pk)
    _assign(setup, setup.other_client.pk)

    for actor in (setup.graph.actor, setup.graph.staff):
        # Vacuity control: a clean submission is findable by its file name.
        clean_hits = _hits(setup, "emitente_cliente", actor)
        assert _ids(clean_hits, SearchKind.DOCUMENT) == {clean.document_id}
        assert any(hit.url.endswith(f"/{clean_link.pk}/revisar/") for hit in clean_hits)

        # The other company's CNPJ, read out of the divergent file, finds that
        # company -- never the document carrying it.
        by_cnpj = _hits(setup, OTHER_CLIENT_CNPJ, actor)
        assert _ids(by_cnpj, SearchKind.CLIENT) == {setup.other_client.pk}
        assert _ids(by_cnpj, SearchKind.DOCUMENT) == set()
        # Nor is it found by its own name while it is suspended as divergent.
        assert _ids(_hits(setup, "outra_empresa", actor), SearchKind.DOCUMENT) == set()
        for q in (OTHER_CLIENT_CNPJ, "outra_empresa", "nfe"):
            body = _body(setup, q, actor)
            assert DIVERGENT_NAME not in body
            assert str(divergent.document_id) not in body


@DB
def test_the_portal_has_no_search_and_a_flag_off_firm_has_none() -> None:
    setup = portal_setup("t21-portal")
    portal = setup.portal()
    assert portal.get("/busca/", {"q": "Synthetic"}).status_code == 404

    setup.graph.tenant.features = []
    setup.graph.tenant.save(update_fields=["features"])
    assert setup.firm().get("/busca/", {"q": "Synthetic"}).status_code == 404


@DB
def test_short_queries_find_nothing_and_each_kind_stops_at_five() -> None:
    setup = portal_setup("t21-limits")
    assert _hits(setup, "S") == []
    assert _hits(setup, "  y ") == []
    with tenant_context(setup.graph.tenant.pk):
        for index in range(7):
            ClientCompany.objects.create(
                tenant=setup.graph.tenant,
                legal_name=f"Limite Cliente {index}",
                cnpj=make_cnpj(f"{index + 20}ABC3450001"),
                status="active",
            )
    hits = _hits(setup, "Limite Cliente")
    assert len(_ids(hits, SearchKind.CLIENT)) == 5
    body = _body(setup, "Limite Cliente")
    # No total is rendered: the fragment lists what it shows and nothing more.
    assert body.count('role="option"') == 5
    assert "Limite Cliente 6" not in body
    assert "data-total" not in body

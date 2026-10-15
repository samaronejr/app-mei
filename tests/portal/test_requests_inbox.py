"""Início lists only the client's own open requests; internals never reach the HTML."""

from http import HTTPStatus

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceRequest, RequestState
from apps.cycles.services.requests import (
    RequestFields,
    create_request,
    waive_request,
)
from tests.portal.request_fixtures import (
    INTERNAL_CANARY,
    OTHER_CLIENT_CANARY,
    portal_setup,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_inbox_lists_own_open_requests_with_due_date_and_s57_copy() -> None:
    setup = portal_setup("inbox-own")
    response = setup.portal().get("/")
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Solicitações do escritório" in body
    assert "Notas de serviço de setembro" in body
    assert "Precisamos das notas emitidas em setembro." in body
    assert f'href="/solicitacoes/{setup.request.pk}/"' in body
    assert setup.request.due_on is not None
    assert setup.request.due_on.isoformat() in body
    assert 'data-status="solicitado">Pendente<' in body
    # The other client's open request is invisible, by RLS (no view filter exists).
    assert OTHER_CLIENT_CANARY not in body
    assert str(setup.other_request.pk) not in body


def test_internal_instructions_and_waiver_reason_are_absent_from_every_page() -> None:
    setup = portal_setup("inbox-internal")
    with tenant_context(setup.graph.tenant.pk):
        extra = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields(title="Extrato bancário", instructions=INTERNAL_CANARY),
            expected_version=setup.cycle.version,
        )
    browser = setup.portal()
    pages = [
        browser.get("/"),
        browser.get(f"/solicitacoes/{setup.request.pk}/"),
    ]
    for page in pages:
        assert page.status_code == HTTPStatus.OK
        assert INTERNAL_CANARY not in page.content.decode()
    # A draft was never sent: not listed, and 404 by id.
    assert "Extrato bancário" not in pages[0].content.decode()
    assert browser.get(f"/solicitacoes/{extra.pk}/").status_code == HTTPStatus.NOT_FOUND
    with tenant_context(setup.graph.tenant.pk):
        waive_request(
            setup.graph.actor,
            setup.request.pk,
            reason="WAIVER-REASON-CANARY",
            expected_version=setup.request.version,
        )
    after = browser.get("/")
    assert "WAIVER-REASON-CANARY" not in after.content.decode()
    assert "Notas de serviço de setembro" not in after.content.decode()


def test_the_inbox_reads_no_internal_table() -> None:
    setup = portal_setup("inbox-sql")
    with CaptureQueriesContext(connection) as queries:
        response = setup.portal().get("/")
    assert response.status_code == HTTPStatus.OK
    sql = " ".join(query["sql"] for query in queries)
    assert "cycles_evidencerequest" in sql
    assert "cycles_requestinternal" not in sql
    assert "cycles_internalnote" not in sql


def test_the_request_policy_confines_the_inbox_not_a_view_filter() -> None:
    """Vacuity control: the other client's request exists and is open."""
    setup = portal_setup("inbox-control")
    with tenant_context(setup.graph.tenant.pk):
        assert EvidenceRequest.objects.filter(
            pk=setup.other_request.pk, state=RequestState.REQUESTED
        ).exists()
    browser = setup.portal()
    assert (
        browser.get(f"/solicitacoes/{setup.other_request.pk}/").status_code
        == HTTPStatus.NOT_FOUND
    )


def test_unflagged_tenant_has_no_inbox_and_no_request_route() -> None:
    setup = portal_setup("inbox-flag")
    setup.graph.tenant.features = []
    setup.graph.tenant.save(update_fields=["features"])
    browser = setup.portal()
    body = browser.get("/").content.decode()
    assert "Solicitações do escritório" not in body
    assert (
        browser.get(f"/solicitacoes/{setup.request.pk}/").status_code
        == HTTPStatus.NOT_FOUND
    )

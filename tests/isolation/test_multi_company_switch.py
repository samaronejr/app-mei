"""S15 J: a contact with two companies switches context explicitly and safely.

* after switching, every page, download and HTMX partial is scoped to the new client;
* switching to a client without an active seat is 403 and changes nothing;
* the session only SELECTS among memberships re-verified on every request: a seat
  revoked after the switch (or a forged session value) falls back to the lowest
  client_id with a notice, never to the stored client;
* the switch rotates the session key.
"""

import re
from http import HTTPStatus
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceLink, EvidenceRequest
from apps.obligations.models import Document
from apps.portal.middleware import SELECTED_CLIENT_SESSION_KEY
from apps.tenants.models import Membership, TenantRole
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

pytestmark = pytest.mark.django_db(transaction=True)

SWITCH = "/empresa/selecionar/"
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
STALE_NOTICE = "Seu acesso à empresa que estava selecionada foi encerrado."


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("switch")
    # A second seat for the same account, in the other client of the same firm.
    Membership.objects.create(
        user=setup.portal_user,
        tenant=setup.graph.tenant,
        client=setup.other_client,
        role=TenantRole.CLIENT_OWNER,
    )
    return setup


def _lowest(setup: PortalSetup) -> ClientCompany:
    return min((setup.graph.client, setup.other_client), key=lambda c: str(c.pk))


def _highest(setup: PortalSetup) -> ClientCompany:
    return max((setup.graph.client, setup.other_client), key=lambda c: str(c.pk))


def _request_of(setup: PortalSetup, company: ClientCompany) -> EvidenceRequest:
    return setup.request if company == setup.graph.client else setup.other_request


def _session_key(browser: Client) -> str:
    return browser.cookies[settings.SESSION_COOKIE_NAME].value


def _assert_scoped_to(
    setup: PortalSetup, browser: Client, company: ClientCompany
) -> None:
    other = setup.other_client if company == setup.graph.client else setup.graph.client
    own_request = _request_of(setup, company)
    foreign_request = _request_of(setup, other)
    home = browser.get("/")
    assert home.status_code == HTTPStatus.OK
    body = home.content.decode()
    heading = H1.search(body)
    assert heading is not None
    assert heading.group(1).strip() == company.legal_name
    assert f'href="/solicitacoes/{own_request.pk}/"' in body
    assert str(foreign_request.pk) not in body
    for path in ("/pagamentos/", "/documentos/"):
        assert browser.get(path).status_code == HTTPStatus.OK
    assert browser.get(f"/solicitacoes/{own_request.pk}/").status_code == HTTPStatus.OK
    assert (
        browser.get(f"/solicitacoes/{foreign_request.pk}/").status_code
        == HTTPStatus.NOT_FOUND
    )
    # HTMX partial: the upload lands on the selected client, and the foreign
    # request is 404 even as an HTMX request.
    partial = browser.post(
        f"/solicitacoes/{own_request.pk}/enviar",
        {"file": upload(f"{company.pk}.pdf", f"bytes of {company.pk}".encode())},
        headers={"HX-Request": "true"},
    )
    assert partial.status_code == HTTPStatus.OK
    assert f"{company.pk}.pdf" in partial.content.decode()
    refused = browser.post(
        f"/solicitacoes/{foreign_request.pk}/enviar",
        {"file": upload("x.pdf", b"x")},
        headers={"HX-Request": "true"},
    )
    assert refused.status_code == HTTPStatus.NOT_FOUND
    with tenant_context(setup.graph.tenant.pk):
        own_doc = Document.objects.get(original_filename=f"{company.pk}.pdf")
        assert own_doc.client_id == company.pk
        link = EvidenceLink.objects.get(document=own_doc)
        assert link.client_id == company.pk
        foreign_docs = list(
            Document.objects.filter(client=other).values_list("storage_key", flat=True)
        )
    # Download: own document served, the other company's never.
    assert browser.get(f"/documentos/{own_doc.storage_key}").status_code == 200
    for key in foreign_docs:
        assert browser.get(f"/documentos/{key}").status_code == HTTPStatus.NOT_FOUND


def test_default_is_the_lowest_client_and_both_companies_are_offered(
    setup: PortalSetup,
) -> None:
    browser = setup.portal()
    body = browser.get("/").content.decode()
    assert 'action="/empresa/selecionar/"' in body
    assert setup.graph.client.legal_name in body
    assert setup.other_client.legal_name in body
    _assert_scoped_to(setup, browser, _lowest(setup))


def test_after_switching_every_surface_is_scoped_to_the_new_client(
    setup: PortalSetup,
) -> None:
    browser = setup.portal()
    before = _session_key(browser)
    target = _highest(setup)
    response = browser.post(SWITCH, {"client": str(target.pk)})
    assert response.status_code == HTTPStatus.FOUND
    assert _session_key(browser) != before
    _assert_scoped_to(setup, browser, target)


def test_switching_to_a_company_without_a_seat_is_403_and_changes_nothing(
    setup: PortalSetup,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        stranger = ClientCompany.objects.create(
            tenant=setup.graph.tenant,
            legal_name="Company without a seat",
            cnpj="11222333000262",
            status="active",
        )
    browser = setup.portal()
    before = _session_key(browser)
    for value in (str(stranger.pk), "not-a-uuid", str(setup.graph.tenant.pk)):
        response = browser.post(SWITCH, {"client": value})
        assert response.status_code == HTTPStatus.FORBIDDEN
        assert _session_key(browser) == before
        assert SELECTED_CLIENT_SESSION_KEY not in browser.session
    _assert_scoped_to(setup, browser, _lowest(setup))


def test_an_inactive_seat_cannot_be_selected(setup: PortalSetup) -> None:
    target = _highest(setup)
    Membership.objects.filter(user=setup.portal_user, client=target).update(
        is_active=False
    )
    browser = setup.portal()
    assert browser.post(SWITCH, {"client": str(target.pk)}).status_code == 403


def test_a_seat_revoked_after_switching_falls_back_with_a_notice(
    setup: PortalSetup,
) -> None:
    browser = setup.portal()
    target = _highest(setup)
    assert browser.post(SWITCH, {"client": str(target.pk)}).status_code == 302
    Membership.objects.filter(user=setup.portal_user, client=target).update(
        is_active=False
    )
    page = browser.get("/")
    assert page.status_code == HTTPStatus.OK
    assert STALE_NOTICE in page.content.decode()
    assert SELECTED_CLIENT_SESSION_KEY not in browser.session
    _assert_scoped_to(setup, browser, _lowest(setup))
    assert STALE_NOTICE not in browser.get("/").content.decode()


def test_a_forged_session_selection_is_never_honoured(setup: PortalSetup) -> None:
    """The session value selects; it never grants. A stored id without a seat is
    ignored even though it names a real client of the same firm."""
    with tenant_context(setup.graph.tenant.pk):
        stranger = ClientCompany.objects.create(
            tenant=setup.graph.tenant,
            legal_name="Forged target",
            cnpj="11222333000343",
            status="active",
        )
    browser = setup.portal()
    session = browser.session
    session[SELECTED_CLIENT_SESSION_KEY] = str(stranger.pk)
    session.save()
    page = browser.get("/")
    assert page.status_code == HTTPStatus.OK
    body = page.content.decode()
    assert "Forged target" not in body
    assert STALE_NOTICE in body
    _assert_scoped_to(setup, browser, _lowest(setup))


def test_unflagged_firm_ignores_the_selection(setup: PortalSetup) -> None:
    browser = setup.portal()
    target = _highest(setup)
    assert browser.post(SWITCH, {"client": str(target.pk)}).status_code == 302
    setup.graph.tenant.features = []
    setup.graph.tenant.save(update_fields=["features"])
    body = browser.get("/").content.decode()
    assert _lowest(setup).legal_name in body
    assert 'action="/empresa/selecionar/"' not in body
    assert browser.post(SWITCH, {"client": str(target.pk)}).status_code == 404

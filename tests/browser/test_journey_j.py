"""S15 J in a real browser: one contact, two companies, an explicit company switch.

The portal opens on the lowest company the account holds a seat in, offers the other
in the header, and after "Trocar empresa" every surface belongs to the chosen company:
its name is the page heading, its requests are listed, the other company's request
is neither listed nor reachable (404), and the session key was rotated.

STAGE 2 (T23): the active-support-grant banner half of S15 J lands with the support
grant surface; this module covers the company switch only.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from pathlib import Path

from django.conf import settings as django_settings
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.cycles.models import EvidenceRequest
from apps.tenants.models import Membership, TenantRole
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import live_graph, open_portal, record
from tests.portal.request_fixtures import PortalSetup, portal_setup

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def _request_of(setup: PortalSetup, company: ClientCompany) -> EvidenceRequest:
    return setup.request if company == setup.graph.client else setup.other_request


def _session(portal: BrowserPage) -> str:
    return next(
        cookie.get("value", "")
        for cookie in portal.page.context.cookies()
        if cookie.get("name") == django_settings.SESSION_COOKIE_NAME
        and "-portal." in cookie.get("domain", "")
    )


def test_journey_j_company_switch_rescopes_every_surface(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("journey-j")
    Membership.objects.create(
        user=setup.portal_user,
        tenant=setup.graph.tenant,
        client=setup.other_client,
        role=TenantRole.CLIENT_OWNER,
    )
    live = live_graph(setup.graph, browser_server.thread.port)
    first, second = sorted(
        (setup.graph.client, setup.other_client), key=lambda company: str(company.pk)
    )
    portal = open_portal(open_page, live, setup.portal_user)
    page = portal.page
    portal.goto(live.url("/", portal=True))
    expect(page.locator("h1")).to_have_text(first.legal_name)
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, first).pk}']")
    ).to_be_visible()
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, second).pk}']")
    ).to_have_count(0)
    switcher = page.get_by_label("Empresa", exact=True)
    expect(switcher).to_have_value(str(first.pk))
    record(portal, "j-01-first-company")

    before = _session(portal)
    switcher.select_option(str(second.pk))
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/empresa/selecionar/")
        )
    ) as switched:
        page.get_by_role("button", name="Trocar empresa").click()
    assert switched.value.status == 302
    expect(page.locator("h1")).to_have_text(second.legal_name)
    expect(switcher).to_have_value(str(second.pk))
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, second).pk}']")
    ).to_be_visible()
    expect(
        page.locator(f"[data-request-id='{_request_of(setup, first).pk}']")
    ).to_have_count(0)
    assert _session(portal) != before
    record(portal, "j-02-second-company")

    foreign = page.goto(
        live.url(f"/solicitacoes/{_request_of(setup, first).pk}/", portal=True)
    )
    assert foreign is not None
    assert foreign.status == 404
    own = _request_of(setup, second)
    portal.goto(live.url(f"/solicitacoes/{own.pk}/", portal=True))
    expect(page.locator("h1")).to_have_text(own.title)
    record(portal, "j-03-second-company-request")
    assert_policed_roles()
    assert portal.console_errors == [
        "Failed to load resource: the server responded with a status of 404 (Not Found)"
    ], portal.console_errors

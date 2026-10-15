"""The client explicitly acknowledges exact bytes; the firm sees that separate fact."""

from collections.abc import Callable

from playwright.sync_api import expect
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.models import ClientAcknowledgement, ClientPublication, CycleEvent
from tests.browser.harness import BrowserPage, LiveFirm, axe_check, mfa_session_cookie
from tests.cycles.publication_fixtures import HOSTILE_BODY, publication_graph
from tests.ui.factories import Firm

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def test_publication_receipt_journey(
    browser_server: LiveServer, open_page: OpenPage
) -> None:

    setup = publication_graph("publication-browser")
    with tenant_context(setup.graph.tenant.pk):
        assert execute(
            setup.graph.actor, setup.proposal.pk, expected_version=2
        ).executed
        publication = ClientPublication.objects.get(proposal=setup.proposal)
    live = LiveFirm(
        Firm(
            tenant=setup.graph.tenant,
            owner=setup.graph.actor,
            accountant=setup.graph.staff,
            clients=[setup.graph.client],
        ),
        browser_server.thread.port,
    )
    portal = open_page(live, setup.portal_user)
    portal.page.context.add_cookies(
        [mfa_session_cookie(setup.portal_user, live.url("/", portal=True))]
    )
    url = live.url(f"/publicacoes/{publication.pk}/", portal=True)
    portal.goto(url)
    expect(portal.page.locator("[data-publication-body]")).to_have_text(HOSTILE_BODY)
    assert portal.page.evaluate("window.T16_CANARY === undefined")
    assert portal.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    portal.screenshot("t16-publication-before-receipt")
    axe_check(portal.page)
    with tenant_context(setup.graph.tenant.pk):
        assert not ClientAcknowledgement.objects.exists()
        assert CycleEvent.objects.filter(
            aggregate_id=publication.pk, type="publication.viewed"
        ).exists()
    with portal.page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as posted:
        portal.page.get_by_role("button", name="Confirmo o recebimento").click()
    assert posted.value.status in {200, 302}
    expect(portal.page.locator("[data-receipt-digest]")).to_have_attribute(
        "data-receipt-digest", publication.publication_digest
    )
    portal.screenshot("t16-publication-receipt")
    with tenant_context(setup.graph.tenant.pk):
        receipt = ClientAcknowledgement.objects.get(publication=publication)
        assert receipt.user_id == setup.portal_user.pk
        assert receipt.publication_digest == publication.publication_digest
    firm = open_page(live, setup.graph.actor)
    firm.goto(live.url("/comunicacao/"))
    expect(firm.page.locator("[data-acknowledgement]")).to_have_attribute(
        "data-acknowledgement", publication.publication_digest
    )
    assert firm.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    firm.screenshot("t16-firm-communication")
    axe_check(firm.page)
    portal.assert_clean_console()
    firm.assert_clean_console()

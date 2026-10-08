"""After an HTMX swap, keyboard focus lands on the heading of the updated region (S5.6).

Both swaps the R1 product makes on a user's request are outerHTML: Hoje's filter
form replaces `#briefing-content`, and the portal upload replaces `#request-status`.
In both the element HTMX swapped is detached afterwards, so focus has to be found in
its replacement. A keyboard user's next Tab continues from the heading, and a screen
reader reads the region's name.

Automações and Suporte swap nothing: their actions are plain forms. For them the
check is the keyboard path itself: the skip link moves focus into the content, Tab
reaches the page's decision control with a visible focus ring, and Enter performs it.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import FilePayload, Page, expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.tenants.support import request_support_grant
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import R1_FLAGS, live_graph, open_portal, record
from tests.portal.request_fixtures import portal_setup

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def test_hoje_filter_swap_focuses_the_queue_heading(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("focus-hoje")
    live = live_graph(setup.graph, browser_server.thread.port, *R1_FLAGS)
    staff = open_page(live, setup.graph.actor)
    page = staff.page
    staff.goto(live.url(f"/?escopo=team&periodo={setup.cycle.period_start:%Y-%m}"))
    page.locator("#briefing-filters").click()
    with page.expect_response(
        lambda response: (
            response.status == 200
            and response.request.headers.get("hx-request") == "true"
        )
    ):
        page.get_by_role("button", name="Aplicar filtros").click()
    expect(page.locator("#briefing-queue")).to_be_focused()
    record(staff, "focus-hoje-after-swap")
    page.keyboard.press("Tab")
    assert page.evaluate(
        "() => document.querySelector('#briefing-content')"
        ".contains(document.activeElement)"
    ), "Tab left the updated region instead of continuing inside it"
    staff.assert_clean_console()
    assert_policed_roles()


def test_portal_upload_swap_focuses_the_status_heading(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("focus-portal")
    live = live_graph(setup.graph, browser_server.thread.port)
    portal = open_portal(open_page, live, setup.portal_user)
    page = portal.page
    portal.goto(live.url(f"/solicitacoes/{setup.request.pk}/", portal=True))
    page.set_input_files(
        "#arquivo",
        files=[
            FilePayload(
                name="nota.pdf", mimeType="application/pdf", buffer=b"%PDF-1.4 foco\n"
            )
        ],
    )
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/solicitacoes/{setup.request.pk}/enviar")
        )
    ):
        page.get_by_role("button", name="Enviar arquivo").click()
    expect(page.locator("#request-status .badge")).to_have_text("Recebido")
    expect(page.locator("#situacao")).to_be_focused()
    record(portal, "focus-portal-after-swap")
    portal.assert_clean_console()
    assert_policed_roles()


TAB_LIMIT = 80


def _keyboard_to(page: Page, name: str) -> None:
    """Skip into the content, then Tab to the named control; fail if never reached."""
    page.keyboard.press("Tab")
    expect(page.locator(".skip-link")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#conteudo")).to_be_focused()
    for _ in range(TAB_LIMIT):
        page.keyboard.press("Tab")
        if page.evaluate("() => document.activeElement.textContent.trim()") == name:
            break
    else:
        msg = f"Tab never reached {name!r}"
        raise AssertionError(msg)
    outline = page.evaluate(
        "() => { const s = getComputedStyle(document.activeElement);"
        " return [s.outlineStyle, s.outlineWidth]; }"
    )
    assert outline[0] != "none", f"{name!r} has focus but no outline: {outline}"
    assert outline[1] != "0px", f"{name!r} has focus but no outline: {outline}"


def test_automations_keyboard_path_pauses_with_visible_focus(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("focus-automacoes")
    live = live_graph(setup.graph, browser_server.thread.port, *R1_FLAGS)
    manager = open_page(live, setup.graph.actor)
    page = manager.page
    manager.goto(live.url("/gestao/automacoes/"))
    expect(page.locator("[data-tenant-paused]")).to_have_attribute(
        "data-tenant-paused", "false"
    )
    _keyboard_to(page, "Pausar envios do escritório")
    record(manager, "focus-automacoes-pause")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith("/gestao/automacoes/")
        )
    ):
        page.keyboard.press("Enter")
    expect(page.locator("[data-tenant-paused]")).to_have_attribute(
        "data-tenant-paused", "true"
    )
    manager.assert_clean_console()
    assert_policed_roles()


def test_support_keyboard_path_approves_with_visible_focus(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("focus-suporte")
    live = live_graph(
        setup.graph, browser_server.thread.port, *R1_FLAGS, "firm_settings_r1"
    )
    staff = User.objects.create_user(email="suporte@foco.example.com", is_staff=True)
    with tenant_context(setup.graph.tenant.pk):
        grant = request_support_grant(
            staff, setup.graph.tenant.pk, purpose="Conferir um envio"
        )
    owner = open_page(live, setup.graph.actor)
    page = owner.page
    owner.goto(live.url("/configuracoes/suporte/"))
    _keyboard_to(page, "Autorizar acesso")
    record(owner, "focus-suporte-approve")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/configuracoes/suporte/{grant.pk}/aprovar/")
        )
    ) as approved:
        page.keyboard.press("Enter")
    assert approved.value.status == 302
    expect(
        page.locator(f"section[aria-labelledby='historico'] [data-grant='{grant.pk}']")
    ).to_contain_text("Aprovado")
    owner.assert_clean_console()
    assert_policed_roles()

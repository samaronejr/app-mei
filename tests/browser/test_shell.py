"""The product shell in a real browser: navigation, keyboard, axe, both viewports.

Every test here runs twice, at 1440x900 and 390x844 (`viewport` in conftest), on
a firm whose tenant carries `cycles_r1`, signed in as its owner by session
injection. Each navigation also proves the live request ran as `app_runtime`
(`BrowserPage.goto`), so a pass here is a pass under the production policies.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import expect

from apps.accounts.models import User
from tests.browser.harness import BrowserPage, LiveFirm, axe_check

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
NAV_NAME = "Navegação principal"
R1_DESTINATIONS = ("Hoje", "Clientes", "Gestão")


def _open_nav(shell: BrowserPage) -> None:
    """On a phone the bar is a closed disclosure; open it the way a person does."""
    nav = shell.page.get_by_role("navigation", name=NAV_NAME)
    if shell.viewport == "mobile":
        expect(nav).to_be_hidden()
        shell.page.get_by_text("Menu", exact=True).click()
    expect(nav).to_be_visible()


def test_the_dashboard_under_the_flag_draws_the_product_shell(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm()
    shell = open_page(live, None)

    shell.goto(live.url("/"))

    header = shell.page.locator('[data-shell="r1"]')
    expect(header).to_be_visible()
    expect(header.get_by_role("link", name="app-mei", exact=True)).to_be_visible()
    expect(header).to_contain_text(live.firm.tenant.name)
    expect(shell.page.locator("details.nav-disclosure")).to_have_count(0)

    _open_nav(shell)
    nav = shell.page.get_by_role("navigation", name=NAV_NAME)
    for label in R1_DESTINATIONS:
        expect(nav.get_by_role("link", name=label, exact=True)).to_be_visible()
    expect(nav.get_by_role("link", name="Hoje", exact=True)).to_have_attribute(
        "aria-current",
        "page",
    )

    axe_check(shell.page)
    shell.assert_clean_console()
    shell.screenshot("shell")


def test_a_staff_accountant_is_not_offered_gestao(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    """The flagged tuple is filtered by capability exactly like the MEI-era one."""
    live = live_firm()
    shell = open_page(live, live.firm.accountant)

    shell.goto(live.url("/"))
    _open_nav(shell)

    nav = shell.page.get_by_role("navigation", name=NAV_NAME)
    expect(nav.get_by_role("link", name="Clientes", exact=True)).to_be_visible()
    expect(nav.get_by_role("link", name="Gestão", exact=True)).to_have_count(0)


def test_the_keyboard_reaches_the_main_landmark_first(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    """Tab lands on the skip link; following it puts focus in <main>."""
    live = live_firm()
    shell = open_page(live, None)
    shell.goto(live.url("/"))

    shell.page.keyboard.press("Tab")
    skip = shell.page.get_by_role("link", name="Pular para o conteúdo")
    expect(skip).to_be_focused()
    expect(skip).to_be_in_viewport()

    shell.page.keyboard.press("Enter")
    expect(shell.page.get_by_role("main")).to_be_focused()

    # The next stop is inside the content, not back in the header.
    shell.page.keyboard.press("Tab")
    focused_in_main = shell.page.locator("main :focus")
    expect(focused_in_main).to_have_count(1)


def test_a_flag_off_firm_keeps_the_mei_era_shell(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm("beta", features=())
    shell = open_page(live, None)

    shell.goto(live.url("/"))

    expect(shell.page.locator('[data-shell="r1"]')).to_have_count(0)
    expect(shell.page.locator("details.nav-disclosure")).to_have_count(1)
    expect(shell.page.get_by_role("link", name="app-mei", exact=True)).to_have_count(0)
    expect(shell.page.locator("header")).to_contain_text(re.compile("Beta"))
    shell.assert_clean_console()

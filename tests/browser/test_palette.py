"""The command palette and the header search field, driven by keyboard alone.

Runs at 1440x900 and 390x844 (`viewport` in conftest) on a `cycles_r1` firm, with
every live request proven to run as `app_runtime` (`BrowserPage.goto`).
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import expect

from apps.accounts.models import User
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.ui.factories import add_client

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
CLIENT_NAME = "Padaria São João"
FIELD_NAME = "Buscar clientes, ciclos, solicitações e documentos"


def _firm_with_client(live_firm: Callable[..., LiveFirm]) -> tuple[LiveFirm, str]:
    live = live_firm()
    company = add_client(live.firm, legal_name=CLIENT_NAME, base="12ABC3450001")
    add_client(live.firm, legal_name="Ferragens Saorense", base="34DEF5670001")
    return live, str(company.pk)


def test_keyboard_only_palette_opens_navigates_and_selects(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live, client_id = _firm_with_client(live_firm)
    shell = open_page(live, None)
    shell.goto(live.url("/"))
    page = shell.page

    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_role("dialog", name="Buscar")
    expect(palette).to_be_visible()
    field = palette.get_by_role("combobox", name=FIELD_NAME)
    expect(field).to_be_focused()

    page.keyboard.type("sao")
    listbox = palette.get_by_role("listbox")
    options = listbox.get_by_role("option")
    expect(options.filter(has_text=CLIENT_NAME)).to_have_count(1)
    expect(field).to_have_attribute("aria-expanded", "true")
    expect(field).not_to_have_attribute("aria-activedescendant", re.compile(".+"))

    page.keyboard.press("ArrowDown")
    first = options.first
    first_id = first.get_attribute("id")
    assert first_id
    expect(field).to_have_attribute("aria-activedescendant", first_id)
    expect(first).to_have_attribute("aria-selected", "true")
    expect(field).to_be_focused()

    page.keyboard.press("ArrowDown")
    second_id = options.nth(1).get_attribute("id")
    assert second_id
    assert second_id != first_id
    expect(field).to_have_attribute("aria-activedescendant", second_id)
    expect(first).to_have_attribute("aria-selected", "false")
    page.keyboard.press("ArrowUp")
    expect(field).to_have_attribute("aria-activedescendant", first_id)

    axe_check(page)
    shell.screenshot("palette")

    page.keyboard.press("Escape")
    expect(palette).to_be_hidden()

    page.keyboard.press("ControlOrMeta+k")
    expect(field).to_be_focused()
    field.fill("")
    page.keyboard.type("padaria sao")
    expect(options.filter(has_text=CLIENT_NAME)).to_have_count(1)
    page.keyboard.press("ArrowDown")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    expect(page).to_have_url(live.url(f"/clientes/{client_id}/"))
    shell.assert_clean_console()


def test_the_header_search_field_is_visible_and_works_without_the_shortcut(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live, client_id = _firm_with_client(live_firm)
    shell = open_page(live, None)
    shell.goto(live.url("/"))
    page = shell.page

    header = page.locator('[data-shell="r1"]')
    form = header.get_by_role("search")
    field = form.get_by_role("searchbox", name="Buscar")
    expect(field).to_be_visible()
    expect(field).to_be_in_viewport()
    shell.screenshot("header-search")

    field.focus()
    page.keyboard.type("joao")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"/busca/\?q=joao$"))
    result = page.get_by_role("main").get_by_role("link", name=re.compile(CLIENT_NAME))
    expect(result).to_be_visible()
    axe_check(page)
    shell.screenshot("search-results")

    result.focus()
    with page.expect_navigation():
        page.keyboard.press("Enter")
    expect(page).to_have_url(live.url(f"/clientes/{client_id}/"))
    shell.assert_clean_console()

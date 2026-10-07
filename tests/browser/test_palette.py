"""The command palette and the header search field, driven by keyboard alone.

Runs at 1440x900 and 390x844 (`viewport` in conftest) on a `cycles_r1` firm, with
every live request proven to run as `app_runtime` (`BrowserPage.goto`). The
long-name reflow check runs on the phone context only, at 390 and 320 px wide.
"""

import re
from collections.abc import Callable
from typing import Any

import pytest
from django.db import models
from playwright.sync_api import Page, expect

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document
from tests.browser.harness import MOBILE, BrowserPage, LiveFirm, axe_check
from tests.ui.factories import add_client

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
CLIENT_NAME = "Padaria São João"
FIELD_NAME = "Buscar clientes, ciclos, solicitações e documentos"
LONG_QUERY = "longunique"
# A name a user may legitimately upload: one unbroken word, nowhere to wrap.
LONG_FILENAME = "Longunique" + "W" * 200 + ".pdf"


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


def _assert_reflows(page: Page, width: int, boxes: str) -> None:
    """Neither the page nor any result box is wider than the screen it is on.

    `innerWidth` is pinned to the device width too: a phone browser lays an
    overflowing page out wider and zooms it out, which would otherwise let the
    page-level comparison pass on a shrunken page.
    """
    measured: dict[str, Any] = page.evaluate(
        """(boxes) => ({
            inner: window.innerWidth,
            scroll: document.documentElement.scrollWidth,
            boxes: Array.from(document.querySelectorAll(boxes), (box) => ({
                box: box.id || box.tagName,
                client: box.clientWidth,
                scroll: box.scrollWidth,
            })),
        })""",
        boxes,
    )
    assert measured["inner"] == width, measured
    assert measured["scroll"] <= measured["inner"], measured
    assert measured["boxes"], measured
    for box in measured["boxes"]:
        assert box["scroll"] <= box["client"], measured


def _narrow_page_with_long_names(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
    width: int,
) -> tuple[BrowserPage, LiveFirm, str]:
    """A firm whose client and document names are single unbroken words.

    The client name is as long as the column allows; the document is a
    standalone one of that client, so the owner may open it.
    """
    field = ClientCompany._meta.get_field("legal_name")
    assert isinstance(field, models.CharField)
    assert field.max_length is not None
    long_client = "Longunique" + "C" * (field.max_length - len("Longunique"))
    live = live_firm()
    client = add_client(live.firm, legal_name=long_client, base="56GHI7890001")
    with tenant_context(live.firm.tenant.pk):
        Document.objects.create(
            tenant=live.firm.tenant,
            client=client,
            uploaded_by=live.firm.owner,
            original_filename=LONG_FILENAME,
            content_type="application/pdf",
            byte_size=1,
            sha256="a" * 64,
        )
    shell = open_page(live, None)
    shell.page.set_viewport_size({"width": width, "height": MOBILE["height"]})
    return shell, live, long_client


@pytest.mark.parametrize("viewport", ["mobile"])
@pytest.mark.parametrize("width", [390, 320])
def test_long_result_names_wrap_in_the_palette_on_narrow_screens(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
    width: int,
) -> None:
    shell, live, long_client = _narrow_page_with_long_names(live_firm, open_page, width)
    page = shell.page
    # Opened over the empty results page, which names no tenant row, so any
    # widening is the palette's own.
    shell.goto(live.url("/busca/"))

    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_role("dialog", name="Buscar")
    expect(palette.get_by_role("combobox", name=FIELD_NAME)).to_be_focused()
    page.keyboard.type(LONG_QUERY)
    options = palette.get_by_role("listbox").get_by_role("option")
    expect(options.filter(has_text=LONG_FILENAME)).to_have_count(1)
    # The client's own option and the document's detail line both carry it.
    expect(options.filter(has_text=long_client)).to_have_count(2)
    _assert_reflows(page, width, "#palette-results, [role=listbox]")
    shell.screenshot(f"palette-long-{width}")
    shell.assert_clean_console()


@pytest.mark.parametrize("viewport", ["mobile"])
@pytest.mark.parametrize("width", [390, 320])
def test_long_result_names_wrap_on_the_results_page_on_narrow_screens(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
    width: int,
) -> None:
    shell, live, long_client = _narrow_page_with_long_names(live_firm, open_page, width)
    page = shell.page
    shell.goto(live.url(f"/busca/?q={LONG_QUERY}"))

    main = page.get_by_role("main")
    expect(main.get_by_role("link", name=LONG_FILENAME, exact=True)).to_be_visible()
    expect(main.get_by_role("link", name=long_client, exact=True)).to_be_visible()
    _assert_reflows(page, width, "main ul")
    shell.screenshot(f"search-long-{width}")
    shell.assert_clean_console()

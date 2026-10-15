"""Real profile, preference and account export flows at both supported viewports."""

import os
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from playwright.sync_api import BrowserType, ConsoleMessage, Error, ViewportSize, expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core import navigation
from apps.core.navigation import NavItem
from apps.core.tenancy import tenant_context
from apps.tenants.export_tasks import build_account_export
from apps.tenants.models import (
    BillingAccount,
    BillingStatus,
    FirmOutboundMessage,
    NotificationPreference,
)
from tests.browser.harness import (
    DESKTOP,
    SCREENSHOT_DIR_ENV,
    BrowserPage,
    LiveFirm,
    axe_check,
    mfa_session_cookie,
)
from tests.ui.factories import add_client, client_base

# With the settings entry beside T21's search field, brand, menu, field, Buscar,
# Configurações and Sair still share the bar's first row at desktop widths, with room
# to spare, on a page whose own menu item is active (bold, wider) as on one without.
# The bar is max-w-7xl, so a classic 15 px scrollbar narrows it only in a window under
# 1280 + 15 px; that loss is charged whether or not this browser draws scrollbars.
HEADER_ROW_VIEWPORTS: Final = (
    ViewportSize(width=1280, height=800),
    ViewportSize(width=1366, height=768),
    ViewportSize(width=1440, height=900),
)
HEADER_ROW_PATHS: Final = ("/configuracoes/", "/clientes/")
HEADER_SPARE_PX: Final = 16
SHELL_BAR_MAX_PX: Final = 1280
CLASSIC_SCROLLBAR_PX: Final = 15
HEADER_GEOMETRY_JS: Final = """() => {
  const box = (selector) => document.querySelector(selector).getBoundingClientRect();
  const nav = box(".shell-disclosure");
  const tops = {
    menu: nav.top,
    field: box("#shell-search-q").top,
    buscar: box('header form.shell-search button[type="submit"]').top,
    settings: box('header .shell-account a[href="/configuracoes/"]').top,
    sair: box("header .shell-account form button").top,
  };
  const bar = getComputedStyle(document.querySelector(".shell-bar"));
  return {
    brandBottom: box(".shell-brand").bottom,
    tops,
    spare: box("header form.shell-search").left - nav.right - parseFloat(bar.columnGap),
    innerWidth: window.innerWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  };
}"""

# Under firm_settings_r1 the firm's name is what gives way (AXR1-022 review). From
# `xl` upward, 2xl included, the bar keeps one row whatever the firm is called --
# Tenant.name holds 255 characters, of wide glyphs or one unbroken word: the name
# shrinks and ends in an ellipsis, keeping its full text for assistive technology
# and in its title, while every action keeps its own width on the first row with
# HEADER_SPARE_PX to spare.
# The classic scrollbar is drawn for real (a Chromium launched without Playwright's
# --hide-scrollbars) rather than charged: the name absorbs those 15 px, which a
# charge on a scrollbar-free render would count twice. One injected menu item stands
# in for the next module (T27).
HEADER_FIT_NAMES: Final = (
    "Reviewer synthetic firm",
    ("WMWMWMW " * 32)[:255],
    ("ContabilidadeHorizonte" * 12)[:255],
)
HEADER_FIT_VIEWPORTS: Final = (
    (ViewportSize(width=1280, height=800), True),
    (ViewportSize(width=1280, height=800), False),
    (ViewportSize(width=1366, height=768), True),
    (ViewportSize(width=1440, height=900), True),
    (ViewportSize(width=1536, height=864), True),
    (ViewportSize(width=1920, height=1080), True),
)
HEADER_FIT_FIXED_ACTIONS: Final = ("field", "buscar", "settings", "sair")
HEADER_FIT_EXTRA_ITEM: Final = NavItem("Automações", "team", "users.create")
# The injected item is drawn where the bar is narrowest (1280 with a scrollbar) and
# where `2xl` begins.
HEADER_FIT_HEADROOM_VIEWPORTS: Final = (
    HEADER_FIT_VIEWPORTS[0],
    HEADER_FIT_VIEWPORTS[4],
)
HEADER_FIT_JS: Final = """() => {
  const header = document.querySelector("header.shell-header");
  const bar = header.querySelector(".shell-bar");
  const box = (element) => element.getBoundingClientRect();
  const tenant = header.querySelector(".shell-tenant");
  const search = header.querySelector("form.shell-search");
  const actions = [
    ...[...header.querySelectorAll(".shell-link")].map(
      (link) => [link.textContent.trim(), link],
    ),
    ["field", header.querySelector("#shell-search-q")],
    ["buscar", search.querySelector('button[type="submit"]')],
    ["settings", header.querySelector('.shell-account a[href="/configuracoes/"]')],
    ["sair", header.querySelector(".shell-account form button")],
  ];
  const brand = header.querySelector(".shell-brand");
  const brandBottom = box(brand).bottom;
  const root = document.documentElement;
  return {
    height: box(header).height,
    spare: box(search).left - box(header.querySelector(".shell-disclosure")).right
      - parseFloat(getComputedStyle(bar).columnGap),
    barOverflow: bar.scrollWidth - bar.clientWidth,
    brandOverflow: brand.scrollWidth - brand.clientWidth,
    pageOverflow: root.scrollWidth - root.clientWidth,
    innerWidth: window.innerWidth,
    clientWidth: root.clientWidth,
    links: header.querySelectorAll(".shell-link").length,
    offRow: actions.filter(([, element]) => box(element).top >= brandBottom)
      .map(([key]) => key),
    widths: Object.fromEntries(
      actions.map(([key, element]) => [key, box(element).width]),
    ),
    title: tenant.title,
    text: tenant.textContent,
    clipped: tenant.scrollWidth > tenant.clientWidth,
    textOverflow: getComputedStyle(tenant).textOverflow,
  };
}"""


def _assert_header_actions_keep_one_row(opened: BrowserPage, live: LiveFirm) -> None:
    if opened.viewport != "desktop":
        return
    page = opened.page
    for viewport in HEADER_ROW_VIEWPORTS:
        page.set_viewport_size(viewport)
        for path in HEADER_ROW_PATHS:
            opened.goto(live.url(path))
            geometry = page.evaluate(HEADER_GEOMETRY_JS)
            where = f"{path} at {viewport['width']}x{viewport['height']}"
            for name, top in geometry["tops"].items():
                assert top < geometry["brandBottom"], (name, where, geometry)
            narrowing = min(SHELL_BAR_MAX_PX, geometry["clientWidth"]) - min(
                SHELL_BAR_MAX_PX, geometry["innerWidth"] - CLASSIC_SCROLLBAR_PX
            )
            spare = geometry["spare"] - max(0, narrowing)
            assert spare >= HEADER_SPARE_PX, (where, geometry)
            assert geometry["scrollWidth"] <= geometry["innerWidth"], (where, geometry)
    page.set_viewport_size(DESKTOP)


def test_profile_preference_and_account_zip(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    tmp_path: Path,
    settings: SettingsWrapper,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    live = live_firm(
        "browser-firm-settings", features=("cycles_r1", "import_r1", "firm_settings_r1")
    )
    add_client(
        live.firm, legal_name="Browser client", base=client_base(1, "browser-settings")
    )
    with tenant_context(live.firm.tenant.pk):
        BillingAccount.objects.create(
            tenant=live.firm.tenant, status=BillingStatus.PAST_DUE
        )
    opened = open_page(live, None)
    page = opened.page
    settings_url = live.url("/configuracoes/")
    opened.goto(settings_url)
    page.locator('[name="name"]').fill("Browser firm saved")
    with page.expect_response(
        lambda response: (
            response.url == settings_url and response.request.method == "POST"
        )
    ) as saved:
        page.locator('main form button[type="submit"]').click()
    assert saved.value.status == 302
    expect(page.locator('[name="name"]')).to_have_value("Browser firm saved")
    live.firm.tenant.refresh_from_db()
    assert live.firm.tenant.name == "Browser firm saved"
    axe_check(page)
    opened.screenshot("firm-settings")
    _assert_header_actions_keep_one_row(opened, live)

    preference_url = live.url("/configuracoes/preferencias/")
    opened.goto(preference_url)
    page.locator('[name="daily_summary"]').check()
    with page.expect_response(
        lambda response: (
            response.url == preference_url and response.request.method == "POST"
        )
    ) as preference_saved:
        page.locator('main form button[type="submit"]').click()
    assert preference_saved.value.status == 302
    expect(page.locator('[name="daily_summary"]')).to_be_checked()
    with tenant_context(live.firm.tenant.pk):
        assert NotificationPreference.objects.get(user=live.firm.owner).daily_summary
        assert not FirmOutboundMessage.objects.exists()

    opened.goto(live.url("/configuracoes/conta/"))
    expect(page.locator("[data-cost-estimate]")).to_be_visible()
    expect(page.locator("[data-billing-past-due]")).to_be_visible()
    axe_check(page)
    opened.screenshot("firm-account")
    with page.expect_response(
        lambda response: (
            response.url == live.url("/configuracoes/exportar/")
            and response.request.method == "POST"
        )
    ) as export_requested:
        page.locator('form[action="/configuracoes/exportar/"] button').click()
    assert export_requested.value.status == 302
    page.wait_for_url("**/configuracoes/exportacoes/*/")
    export_id = UUID(page.url.rstrip("/").split("/")[-1])
    build_account_export(str(live.firm.tenant.pk), str(export_id))
    page.reload()
    with page.expect_download() as downloaded:
        page.locator(f'a[href$="/{export_id}/arquivo/"]').click()
    destination = tmp_path / "account.zip"
    downloaded.value.save_as(destination)
    with zipfile.ZipFile(destination) as archive:
        assert "documents.csv" in archive.namelist()
        assert "manifest.sha256" in archive.namelist()
        assert live.firm.clients[0].cnpj.encode() in archive.read("clients.csv")
    opened.assert_clean_console()


@contextmanager
def _classic_scrollbar_page(
    browser_type: BrowserType, launch_args: dict[str, Any], live: LiveFirm
) -> Iterator[BrowserPage]:
    """Sign the owner in on a desktop Chromium that draws a classic scrollbar.

    Playwright starts headless Chromium with --hide-scrollbars. A window that draws
    the classic 15 px bar is the narrowest real desktop layout, so this one is
    launched without that switch, and closed with the test.
    """
    browser = browser_type.launch(
        **launch_args, ignore_default_args=["--hide-scrollbars"]
    )
    try:
        context = browser.new_context(
            viewport=DESKTOP, locale="pt-BR", timezone_id="America/Sao_Paulo"
        )
        context.add_cookies([mfa_session_cookie(live.firm.owner, live.url("/"))])
        opened = BrowserPage(page=context.new_page(), viewport="desktop")

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                opened.console_errors.append(message.text)

        def on_page_error(error: Error) -> None:
            opened.console_errors.append(f"uncaught: {error}")

        opened.page.on("console", on_console)
        opened.page.on("pageerror", on_page_error)
        yield opened
    finally:
        browser.close()


@dataclass
class _HeaderFitRun:
    """Render the flag-on bar for each firm name and record what breaks."""

    pages: dict[bool, BrowserPage]
    live: LiveFirm
    flag_off_height: float
    short_name_widths: dict[tuple[int, bool, str], dict[str, float]] = field(
        default_factory=dict
    )
    rows: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def rename(self, name: str) -> None:
        tenant = self.live.firm.tenant
        tenant.name = name
        tenant.save(update_fields=["name"])

    def size(self, size: ViewportSize, *, scrollbar: bool) -> None:
        self.pages[scrollbar].page.set_viewport_size(size)

    def check(
        self,
        index: int,
        path: str,
        size: ViewportSize,
        *,
        scrollbar: bool,
        extra_item: bool = False,
    ) -> None:
        name = HEADER_FIT_NAMES[index]
        drawn = "shown" if scrollbar else "hidden"
        case = f"{'extra-item-' if extra_item else ''}name{index}-{path.strip('/')}"
        where = f"{case} {size['width']}x{size['height']} {drawn}"
        opened = self.pages[scrollbar]
        page = opened.page
        opened.goto(self.live.url(path))
        page.evaluate("() => { document.documentElement.style.overflowY = 'scroll'; }")
        geometry: dict[str, Any] = page.evaluate(HEADER_FIT_JS)
        # The case is only what it says when the scrollbar really is (or is not) drawn.
        drawn_px = CLASSIC_SCROLLBAR_PX if scrollbar else 0
        assert geometry["clientWidth"] == geometry["innerWidth"] - drawn_px, geometry
        opened.screenshot(f"header-fit-{case}-{size['width']}-{drawn}")
        key = (size["width"], scrollbar, path)
        if index == 0 and not extra_item:
            self.short_name_widths[key] = geometry["widths"]
        short = self.short_name_widths[key]
        problems = _header_fit_problems(
            geometry,
            name=name,
            flag_off_height=self.flag_off_height,
            widths=short if index else None,
        )
        links = len(short) - len(HEADER_FIT_FIXED_ACTIONS) + int(extra_item)
        if geometry["links"] != links:
            problems.append(f"{geometry['links']} menu items drawn, not {links}")
        self.rows.append(f"{where} spare={geometry['spare']} {problems or 'OK'}")
        self.failures.extend(f"{where}: {problem}" for problem in problems)

    def report(self) -> str:
        table = "\n".join(self.rows)
        directory = os.environ.get(SCREENSHOT_DIR_ENV)
        if directory:
            Path(directory, "header-fit.txt").write_text(table + "\n")
        return "\n".join([*self.failures, "", table])


def _header_fit_problems(
    geometry: dict[str, Any],
    *,
    name: str,
    flag_off_height: float,
    widths: dict[str, float] | None,
) -> list[str]:
    """Name every way this render breaks the one-row, name-gives-way contract."""
    problems = []
    if geometry["offRow"]:
        problems.append(f"off the first row: {geometry['offRow']}")
    if geometry["height"] != flag_off_height:
        problems.append(f"height {geometry['height']} != flag-off {flag_off_height}")
    if geometry["spare"] < HEADER_SPARE_PX:
        problems.append(f"spare {geometry['spare']} < {HEADER_SPARE_PX}")
    if geometry["barOverflow"] > 0 or geometry["pageOverflow"] > 0:
        problems.append(
            f"horizontal overflow bar={geometry['barOverflow']} "
            f"page={geometry['pageOverflow']}"
        )
    if geometry["brandOverflow"] > 0:
        problems.append(f"brand content spills by {geometry['brandOverflow']} px")
    if geometry["title"] != name or geometry["text"] != name:
        problems.append("the firm's full name is not its title and text")
    if widths is not None:
        if not geometry["clipped"] or geometry["textOverflow"] != "ellipsis":
            problems.append(
                f"long name not ended with an ellipsis: clipped={geometry['clipped']} "
                f"text-overflow={geometry['textOverflow']}"
            )
        shrunk = {
            key: (width, geometry["widths"][key])
            for key, width in widths.items()
            if abs(geometry["widths"][key] - width) > 0.5
        }
        if shrunk:
            problems.append(f"actions changed width (short name, now): {shrunk}")
    return problems


def test_firm_name_gives_way_to_header_actions(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    monkeypatch: pytest.MonkeyPatch,
    browser_type: BrowserType,
    browser_type_launch_args: dict[str, Any],
) -> None:
    live = live_firm("browser-header-fit", features=("cycles_r1", "firm_settings_r1"))
    opened = open_page(live, None)
    if opened.viewport != "desktop":
        # Below `xl` the bar wraps as it always has; at 390 px a long name still ends
        # inside the page.
        for name in HEADER_FIT_NAMES:
            live.firm.tenant.name = name
            live.firm.tenant.save(update_fields=["name"])
            opened.goto(live.url("/clientes/"))
            geometry = opened.page.evaluate(HEADER_FIT_JS)
            assert geometry["pageOverflow"] <= 0, (name, geometry)
            assert geometry["title"] == geometry["text"] == name, geometry
        opened.assert_clean_console()
        return

    # The flag-off bar (T21's, unchanged) keeps one row only for a name as short as
    # the reviewer's, so its height is read with that name.
    reference = live_firm("browser-header-fit-off")
    reference.firm.tenant.name = HEADER_FIT_NAMES[0]
    reference.firm.tenant.save(update_fields=["name"])
    flag_off = open_page(reference, None)
    flag_off.goto(reference.url("/clientes/"))
    flag_off_height = flag_off.page.evaluate(
        "() => document.querySelector('header.shell-header')"
        ".getBoundingClientRect().height"
    )
    with _classic_scrollbar_page(browser_type, browser_type_launch_args, live) as bar:
        run = _HeaderFitRun(
            pages={False: opened, True: bar}, live=live, flag_off_height=flag_off_height
        )
        for index, name in enumerate(HEADER_FIT_NAMES):
            run.rename(name)
            for size, scrollbar in HEADER_FIT_VIEWPORTS:
                run.size(size, scrollbar=scrollbar)
                for path in HEADER_ROW_PATHS:
                    run.check(index, path, size, scrollbar=scrollbar)
            brand = opened.page.locator("header .shell-brand").aria_snapshot()
            if name not in brand:
                run.failures.append(f"name{index}: full name not in a11y tree: {brand}")

        # Headroom: one more menu item than the bar holds today still keeps the row.
        monkeypatch.setattr(
            navigation, "NAV_R1", (*navigation.NAV_R1, HEADER_FIT_EXTRA_ITEM)
        )
        for size, scrollbar in HEADER_FIT_HEADROOM_VIEWPORTS:
            run.size(size, scrollbar=scrollbar)
            for index, name in enumerate(HEADER_FIT_NAMES):
                run.rename(name)
                for path in HEADER_ROW_PATHS:
                    run.check(index, path, size, scrollbar=scrollbar, extra_item=True)
        bar.assert_clean_console()
    opened.page.set_viewport_size(DESKTOP)
    report = run.report()
    assert not run.failures, report
    opened.assert_clean_console()


def test_staff_settings_do_not_render_owner_billing_or_profile_controls(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm(
        "browser-private-settings", features=("cycles_r1", "firm_settings_r1")
    )
    with tenant_context(live.firm.tenant.pk):
        BillingAccount.objects.create(
            tenant=live.firm.tenant, status=BillingStatus.PAST_DUE
        )
    opened = open_page(live, live.firm.accountant)
    opened.goto(live.url("/configuracoes/"))
    expect(opened.page.locator("[data-billing-past-due]")).to_have_count(0)
    expect(opened.page.locator('[name="brand_name"]')).to_have_count(0)
    expect(opened.page.locator('a[href="/configuracoes/conta/"]')).to_have_count(0)
    expect(
        opened.page.locator('a[href="/configuracoes/preferencias/"]')
    ).to_be_visible()
    axe_check(opened.page)
    opened.assert_clean_console()


def test_support_trail_renders_for_the_owner_at_both_viewports(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    """The B5 audit page renders the grant's request rows for its owner."""
    from apps.tenants.support import (  # noqa: PLC0415 — test-local imports
        SupportApproval,
        approve_support_grant,
        request_support_grant,
    )
    from apps.tenants.support_models import SupportScope  # noqa: PLC0415
    from tests.support import enrol_totp  # noqa: PLC0415

    live = live_firm("browser-supt-trail", features=("cycles_r1", "firm_settings_r1"))
    # A short tenant name keeps the header-row geometry check name-insensitive:
    # the bar's spare margin was measured at 30.3px on both the parent and this
    # head, and the helper needs >= 31px once it subtracts the simulated
    # 15px scrollbar at 1280 (see .evidence/AXR1-023-fix.md parity numbers).
    live.firm.tenant.name = "Trilha"
    with tenant_context(live.firm.tenant.pk):
        live.firm.tenant.save(update_fields=["name"])
    staff = User.objects.create_user(
        email="browser-staff@platform.example.com",
        password="unused-password",  # noqa: S106
        is_staff=True,
    )
    enrol_totp(staff)
    with tenant_context(live.firm.tenant.pk):
        grant = request_support_grant(
            staff, live.firm.tenant.pk, purpose="Browser trail"
        )
        approve_support_grant(
            live.firm.owner,
            live.firm.tenant.pk,
            grant.pk,
            SupportApproval(scope=SupportScope.READ_ONLY, hours=4, client_ids=()),
        )
    # The staff visit under the grant is what the trail lists as a request row.
    staff_page = open_page(live, staff)
    staff_page.goto(live.url("/"))
    opened = open_page(live, None)
    page = opened.page
    opened.goto(live.url("/configuracoes/suporte/"))
    # Follow the trail link on the decided grant card.
    page.locator('a[href*="/trilha/"]').first.click()
    expect(page.locator("h2#trilha")).to_be_visible()
    expect(page.locator("table tbody tr").first).to_be_visible()
    expect(page.locator("td", has_text="Requisição").first).to_be_visible()
    axe_check(page)
    opened.screenshot("support-trail")
    opened.assert_clean_console()
    _assert_header_actions_keep_one_row(opened, live)

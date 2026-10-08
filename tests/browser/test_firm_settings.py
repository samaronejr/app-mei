"""Real profile, preference and account export flows at both supported viewports."""

import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Final
from uuid import UUID

from playwright.sync_api import ViewportSize, expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.tenants.export_tasks import build_account_export
from apps.tenants.models import (
    BillingAccount,
    BillingStatus,
    FirmOutboundMessage,
    NotificationPreference,
)
from tests.browser.harness import DESKTOP, BrowserPage, LiveFirm, axe_check
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

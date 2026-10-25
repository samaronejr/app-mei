"""Provider measurement states remain readable in the real owner account panel."""

from collections.abc import Callable
from uuid import uuid4

from django.utils import timezone
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.tenants.models import UsageRecord
from tests.browser.harness import BrowserPage, LiveFirm, axe_check


def test_owner_provider_usage_panel(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
) -> None:
    # Given measured SERPRO checks, estimated Focus captures, and a missing metric.
    live = live_firm(
        "provider-usage-browser",
        features=(
            "cycles_r1",
            "firm_settings_r1",
            "scan_required",
            "federal_checks",
            "document_capture",
        ),
    )
    settings.USAGE_UNIT_PRICES_BRL = {
        "serpro_consulta": "0.24",
        "focus_documentos": "1",
    }
    with tenant_context(live.firm.tenant.pk):
        for metric, quantity, kind in (
            ("serpro_consulta", 3, "confirmed"),
            ("focus_documentos", 7, "estimated"),
        ):
            UsageRecord.objects.create(
                tenant=live.firm.tenant,
                metric=metric,
                quantity=quantity,
                kind=kind,
                period=timezone.localdate(),
                source_event=uuid4(),
            )
    # When the owner opens the existing usage panel at each supported viewport.
    opened = open_page(live, None)
    opened.goto(live.url("/configuracoes/conta/"))
    table = opened.page.locator("[data-cost-estimate]")
    expect(table).to_be_visible()
    # Then real quantities and the incomplete total are accessible without overflow.
    expect(table.locator("tbody tr")).to_have_count(8)
    assert opened.page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth"
    )
    axe_check(opened.page)
    opened.screenshot("AXRM-010-usage")
    opened.assert_clean_console()

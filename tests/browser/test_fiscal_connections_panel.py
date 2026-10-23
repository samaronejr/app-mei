"""AXRM-007: the read-only "Conexoes fiscais" panel at 1440x900 and 390x844, with axe.

The owner of a firm with a regional flag on sees a simulated SERPRO connection and a
Focus homologation connection, each with its environment and a status in which the
platform pause wins; nothing is painted as verified. On a phone the table regrows its
column names out of `data-label`.
"""

from collections.abc import Callable

from playwright.sync_api import expect

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.integrations.models import ProviderConnection
from tests.browser.harness import BrowserPage, LiveFirm, axe_check

FLAGS = ("cycles_r1", "firm_settings_r1", "scan_required", "federal_checks")


def test_the_owner_reads_fiscal_connections_with_environment_and_pause(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm("browser-conexoes", features=FLAGS)
    with tenant_context(live.firm.tenant.pk):
        ProviderConnection.objects.create(
            tenant=live.firm.tenant,
            provider="serpro",
            environment="fake",
            status="ativa",
        )
        ProviderConnection.objects.create(
            tenant=live.firm.tenant,
            provider="focus",
            environment="homologacao",
            status="credenciais_ausentes",
        )
    opened = open_page(live, None)
    opened.goto(live.url("/configuracoes/"))
    panel = opened.page.locator("[data-fiscal-connections]")

    expect(panel).to_be_visible()
    expect(panel.locator("caption")).to_have_text("Conexões fiscais da firma")
    expect(panel.locator("tbody tr")).to_have_count(2)
    expect(panel.get_by_text("Simulado")).to_be_visible()
    expect(panel.get_by_text("Homologação")).to_be_visible()
    expect(panel.get_by_text("Pausada pela plataforma")).to_have_count(2)
    expect(panel.locator('[data-state="confirmed"]')).to_have_count(0)
    expect(panel.locator("form")).to_have_count(0)
    axe_check(opened.page)
    opened.screenshot("AXRM-007-fiscal-connections")
    opened.assert_clean_console()


def test_a_staff_accountant_never_sees_the_panel(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm("browser-conexoes-staff", features=FLAGS)
    with tenant_context(live.firm.tenant.pk):
        ProviderConnection.objects.create(
            tenant=live.firm.tenant, provider="serpro", environment="fake"
        )
    opened = open_page(live, live.firm.accountant)
    opened.goto(live.url("/configuracoes/"))

    expect(opened.page.locator("[data-fiscal-connections]")).to_have_count(0)
    axe_check(opened.page)
    opened.assert_clean_console()

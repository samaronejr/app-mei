"""AXRM-008's staff journey, real forms and private evidence at both widths."""

from collections.abc import Callable

from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.ui.factories import add_client

FLAGS = ("cycles_r1", "scan_required", "federal_checks", "document_capture")


def test_declare_verify_and_revoke_authority(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
) -> None:
    settings.CLAMAV_ENABLED = True
    live = live_firm("browser-authority", features=FLAGS)
    live.firm.owner.full_name = "Ana"
    live.firm.owner.save(update_fields=["full_name"])
    company = add_client(
        live.firm, legal_name="Cliente demonstrativo", base="112223330001"
    )
    opened = open_page(live, None)
    opened.goto(live.url(f"/clientes/{company.pk}/"))
    panel = opened.page.locator("[data-authority-panel]")
    expect(panel).to_be_visible()
    row = panel.locator('[data-authority-service="pagtoweb"]')
    row.get_by_label("Válida desde").fill("2026-01-01")
    row.get_by_label("Válida até").fill("2027-12-31")
    row.get_by_label("Evidência PDF").set_input_files(
        {
            "name": "procuracao.pdf",
            "mimeType": "application/pdf",
            "buffer": b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF",
        },
    )
    row.get_by_role("button", name="Registrar declaração").click()
    expect(row.get_by_text("Declarada (evidência anexada)", exact=True)).to_be_visible()
    expect(row.get_by_role("link", name="Ver evidência")).to_be_visible()
    opened.screenshot("AXRM-008-happy-declared")
    row.get_by_role("button", name="Verificar autorização").click()
    expect(row.get_by_text("Verificada por Ana em", exact=False)).to_be_visible()
    opened.screenshot("AXRM-008-happy-verified")
    row.get_by_role("button", name="Revogar autorização").click()
    expect(row.get_by_text("Revogada", exact=True)).to_be_visible()
    opened.screenshot("AXRM-008-failure-revoked")
    expect(panel.locator('[data-state="confirmed"]')).to_have_count(0)
    axe_check(opened.page)
    opened.assert_clean_console()
    from apps.integrations.authority import authority_for  # noqa: PLC0415

    with tenant_context(live.firm.tenant.pk):
        assert authority_for(company, "SP-PAG").state == "missing"

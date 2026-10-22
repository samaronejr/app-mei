"""Registry validation through the real CSV wizard and workspace."""

from collections.abc import Callable

import pytest
from playwright.sync_api import FilePayload, expect

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.clients.tasks import commit_import_task
from apps.core.tenancy import tenant_context
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.ui.factories import client_base, make_cnpj

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("code", ["3303302", "3399999"])
def test_municipality_import(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    live = live_firm("axrm006", features=("import_r1", "cycles_r1"))
    shell = open_page(live, None)
    page = shell.page
    queued: list[tuple[str, ...]] = []
    monkeypatch.setattr(commit_import_task, "delay", lambda *args: queued.append(args))
    shell.goto(live.url("/clientes/importar/"))
    page.locator('input[type="file"]').set_input_files(
        FilePayload(
            name="municipios.csv",
            mimeType="text/csv",
            buffer=(
                "CNPJ;Razão social;UF;IBGE\n"
                f"{make_cnpj(client_base(1, 'axrm006'))};Cliente municipal;RJ;{code}\n"
            ).encode(),
        ),
    )
    with page.expect_navigation():
        page.get_by_role("button", name="Gerar prévia").click()
    if code == "3399999":
        expect(page.locator('[data-count="invalid"]')).to_have_text("1")
        expect(page.locator('[data-count="created"]')).to_have_text("0")
        expect(
            page.get_by_text("Município não encontrado no cadastro (código IBGE)")
        ).to_be_visible()
        with tenant_context(live.firm.tenant.pk):
            assert not ClientCompany.objects.exists()
        shell.screenshot("AXRM-006-failure")
    else:
        expect(page.locator('[data-count="created"]')).to_have_text("1")
        with page.expect_navigation():
            page.get_by_role("button", name="Importar 1 cliente").click()
        assert len(queued) == 1
        commit_import_task.apply(args=list(queued[0])).get()
        shell.goto(page.url)
        with page.expect_navigation():
            page.get_by_role("link", name="Cliente municipal", exact=True).click()
        expect(page.locator("main header")).to_contain_text("Niterói/RJ")
        shell.screenshot("AXRM-006-happy")
    axe_check(page)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shell.assert_clean_console()

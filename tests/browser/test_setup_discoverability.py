"""AXR1-047 in a real browser: import a client, then configure it by clicking.

Runs at 1440x900 and 390x844 (the `viewport` fixture parametrizes `open_page`) on a
firm carrying `cycles_r1` and `import_r1`, signed in as its owner. The whole point
is that no URL is typed: the import result is reached from the wizard, the client
from the import result, and `/clientes/<id>/cadastro/` from the client's workspace —
and the last hop is made with the keyboard, from the focused link, because a control
that only answers a mouse is not an entry point for everyone.

Each screen is held to axe (serious/critical fail), checked for horizontal overflow
at the viewport it is being drawn in, and captured.

The commit is the one step the browser cannot finish on its own: it is queued as a
`TenantTask` and this process runs no worker, so `delay` is captured and the task is
executed in-process before the result page is re-requested. That is the same seam
`tests/clients/test_import_wizard.py` uses, and it keeps the request path real.
"""

import re
from collections.abc import Callable
from typing import Any, Final

import pytest
from playwright.sync_api import FilePayload, expect

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.clients.tasks import commit_import_task
from apps.core.tenancy import tenant_context
from tests.browser.harness import (
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
    axe_check,
)
from tests.ui.factories import client_base, make_cnpj

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
FEATURES: Final = ("cycles_r1", "import_r1")
SLUG: Final = "t47"
IMPORTED_NAME: Final = "Mercearia Importada MEI"
NO_OVERFLOW: Final = "document.documentElement.scrollWidth <= innerWidth"
SETUP_SECTIONS: Final = ("#servicos", "#perfil", "#contatos", "#responsaveis")


def _payload(cnpj: str) -> FilePayload:
    """Build a one-row CSV whose headers the wizard already recognizes."""
    rows = ["CNPJ;Razão social", f"{cnpj};{IMPORTED_NAME}"]
    return FilePayload(
        name="carteira.csv",
        mimeType="text/csv",
        buffer="\n".join(rows).encode(),
    )


def test_the_owner_imports_then_reaches_the_setup_page_by_clicking(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = live_firm(SLUG, features=FEATURES)
    shell = open_page(live, None)
    page = shell.page
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(commit_import_task, "delay", lambda *args: queued.append(args))

    shell.goto(live.url("/clientes/"))
    page.get_by_role("link", name="Importar clientes").click()
    page.get_by_label(re.compile(r"^Arquivo CSV ou XLSX( \*)?$")).set_input_files(
        _payload(make_cnpj(client_base(1, SLUG))),
    )
    with page.expect_navigation():
        page.get_by_role("button", name="Gerar prévia").click()
    expect(page.locator('[data-count="created"]')).to_have_text("1")
    axe_check(page)
    shell.screenshot("AXR1-047-import-preview")

    with page.expect_navigation():
        page.get_by_role("button", name="Importar 1 cliente").click()
    assert queued, "the commit view queued no import task"
    commit_import_task.apply(args=list(queued[0])).get()
    shell.goto(page.url)
    with tenant_context(live.firm.tenant.id):
        company = ClientCompany.objects.get(import_batch__isnull=False)
    assert company.legal_name == IMPORTED_NAME

    created = page.get_by_role("link", name=IMPORTED_NAME)
    expect(created).to_be_visible()
    created.focus()
    expect(created).to_be_focused()
    axe_check(page)
    assert page.evaluate(NO_OVERFLOW), f"import result overflows at {shell.viewport}"
    shell.screenshot("AXR1-047-import-result")

    with page.expect_navigation():
        created.click()
    expect(page).to_have_url(re.compile(rf"/clientes/{company.pk}/$"))
    setup_link = page.get_by_role("link", name="Cadastro, contatos e acessos")
    expect(setup_link).to_be_visible()
    setup_link.focus()
    expect(setup_link).to_be_focused()
    axe_check(page)
    assert page.evaluate(NO_OVERFLOW), f"workspace overflows at {shell.viewport}"
    shell.screenshot("AXR1-047-workspace")

    with page.expect_navigation():
        page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(rf"/clientes/{company.pk}/cadastro/$"))
    for section in SETUP_SECTIONS:
        expect(page.locator(section)).to_be_visible()
    axe_check(page)
    assert page.evaluate(NO_OVERFLOW), f"setup page overflows at {shell.viewport}"
    shell.screenshot("AXR1-047-setup")

    assert_policed_roles()
    shell.assert_clean_console()

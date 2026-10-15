"""The `partials/ui/` components in a real browser, inside the product shell.

The kit page (`tests/browser/templates/component_kit.html`, served only by the
browser URLconf) renders every component once with realistic pt-BR data. These
tests drive the behaviour the Alpine components and `static/js/app.js` add --
focus restore, the upload list's refusals, the unsaved-changes guard, the swap
focus marker, toast dismissal -- and hold the whole kit to axe at both widths.

The last test is the axe helper's positive control: a page with a control that
has no accessible name must make `axe_check` fail, or every "0 violations" in
this suite is unproven.
"""

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import FilePayload, expect

from apps.accounts.models import User
from tests.browser.harness import AxeViolationsError, BrowserPage, LiveFirm, axe_check

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
KIT = "/__ui__/kit/"
PDF = "application/pdf"


def _kit(live_firm: Callable[..., LiveFirm], open_page: OpenPage) -> BrowserPage:
    live = live_firm()
    shell = open_page(live, None)
    shell.goto(live.url(KIT))
    # The trigger is revealed by the dialog component, so this is also the signal
    # that Alpine started and registered every component on this page.
    expect(shell.page.get_by_role("button", name="Revisar e aprovar")).to_be_visible()
    return shell


def test_every_component_passes_axe_inside_the_shell(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    shell = _kit(live_firm, open_page)

    expect(shell.page.locator("[data-kit=badges] .status-badge")).to_have_count(8)
    axe_check(shell.page)
    shell.assert_clean_console()
    shell.screenshot("kit")


def test_a_dialog_returns_focus_to_the_control_that_opened_it(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    shell = _kit(live_firm, open_page)
    page = shell.page

    trigger = page.get_by_role("button", name="Revisar e aprovar")
    trigger.click()
    dialog = page.get_by_role("dialog", name="Aprovar envio")
    expect(dialog).to_be_visible()
    expect(dialog.locator(":focus")).to_have_count(1)
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()

    opener = page.get_by_role("button", name="Ver documento")
    opener.click()
    drawer = page.get_by_role("dialog", name="Documento recebido")
    expect(drawer).to_be_visible()
    drawer.get_by_role("button", name="Fechar").click()
    expect(drawer).to_be_hidden()
    expect(opener).to_be_focused()
    shell.assert_clean_console()


def test_a_drawer_reached_by_its_url_opens_modal_on_load(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    """Rendered open by the server, upgraded to modal once the component runs."""
    live = live_firm()
    shell = open_page(live, None)
    shell.goto(live.url(f"{KIT}?gaveta=1"))

    drawer = shell.page.get_by_role("dialog", name="Documento recebido")
    expect(drawer).to_be_visible()
    expect(drawer.get_by_role("button", name="Fechar")).to_be_visible()
    assert drawer.evaluate("dialog => dialog.matches(':modal')")
    shell.page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    shell.assert_clean_console()


def test_the_upload_list_refuses_oversized_and_unaccepted_files(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    shell = _kit(live_firm, open_page)
    page = shell.page
    picker = page.get_by_label("Notas de serviço de março")

    picker.set_input_files(
        [
            FilePayload(name="nota-0002.pdf", mimeType=PDF, buffer=b"%PDF-1.4 ok"),
            FilePayload(name="extrato.pdf", mimeType=PDF, buffer=b"0" * 4096),
            FilePayload(name="foto.png", mimeType="image/png", buffer=b"\x89PNG"),
        ],
    )

    expect(page.locator("[data-upload-state]")).to_have_count(3)
    expect(page.locator('[data-upload-state="ready"]')).to_have_count(1)
    expect(page.locator('[data-upload-state="refused"]')).to_have_count(2)
    expect(page.get_by_text("Arquivo acima do limite de 2 KB.")).to_be_visible()
    expect(page.get_by_text("Tipo de arquivo não aceito.")).to_be_visible()
    expect(page.locator("#kit-arquivos_status")).to_have_text(
        "1 arquivo pronto para enviar.",
    )
    # Refused files are gone from the input itself, so they are never uploaded.
    assert picker.evaluate("input => input.files.length") == 1

    page.get_by_role("button", name="Remover nota-0002.pdf").click()
    expect(page.locator("[data-upload-state]")).to_have_count(2)
    expect(page.locator("#kit-arquivos_status")).to_have_text("")
    assert picker.evaluate("input => input.files.length") == 0
    shell.assert_clean_console()


def test_an_edited_form_asks_before_the_page_is_left(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    shell = _kit(live_firm, open_page)
    page = shell.page

    page.get_by_label("Observação para o cliente").fill("Precisamos das notas.")

    with page.expect_event("dialog") as asked:
        page.close(run_before_unload=True)
    assert asked.value.type == "beforeunload"
    asked.value.dismiss()


def test_a_swap_moves_focus_to_the_element_the_server_marked(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    shell = _kit(live_firm, open_page)
    page = shell.page

    with page.expect_response(lambda response: response.url.endswith("/kit/swap/")):
        page.get_by_role("button", name="Carregar próxima pendência").click()

    marked = page.get_by_role("link", name="Revisar documento")
    expect(marked).to_be_focused()
    # Consumed once honoured, so the next unrelated swap cannot pull focus back.
    expect(marked).not_to_have_attribute("data-focus-after-swap", re.compile(".*"))
    expect(page.locator("#htmx-announce")).to_have_text("Próxima pendência")
    shell.assert_clean_console()


def test_a_toast_is_dismissed_by_its_own_control(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    shell = _kit(live_firm, open_page)
    page = shell.page

    toast = page.locator("[data-toast]")
    expect(toast).to_contain_text("Mensagem aceita pelo servidor de e-mail.")
    toast.get_by_role("button", name="Dispensar aviso").click()
    expect(toast).to_have_count(0)


def test_axe_check_refuses_a_control_with_no_accessible_name(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    """Positive control: the helper must be able to fail."""
    live = live_firm()
    shell = open_page(live, None)
    shell.goto(live.url("/__ui__/unnamed-button/"))
    expect(shell.page.locator("[data-kit=unnamed]")).to_be_visible()

    with pytest.raises(AxeViolationsError, match="button-name"):
        axe_check(shell.page)

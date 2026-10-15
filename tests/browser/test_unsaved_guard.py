"""AXR1-040 in a real browser: the unsaved-changes guard on product editing forms.

Runs at 1440x900 and 390x844 (the `viewport` fixture parametrizes `open_page`),
signed in as the firm owner. The guard is the browser's own `beforeunload` prompt,
so every departure below is a real navigation made the way that viewport's user
makes one: Enter on the focused link at desktop, a touch tap at mobile. That same
gesture is the user activation Chromium requires before it will prompt at all, so
a page that does not ask was put exactly the question a page that asks was put.

Each prompt is subscribed to (`expect_event`) before the gesture that raises it,
and answered by a handler registered before that: an unanswered dialog freezes the
input that raised it. The guard is ready when a navigation reports `load`, because
the deferred scripts -- and Alpine's start, a microtask after its own -- all run
before `DOMContentLoaded`. Nothing here sleeps.

A refused save answers 422 by design (plan S7), which Chromium logs as a failed
resource; every other console error fails the test.
"""

import re
from collections.abc import Callable
from typing import Final

import pytest
from django.conf import settings
from django.urls import reverse
from playwright.sync_api import Dialog, Locator, expect

from apps.accounts.models import User
from apps.clients.maintenance import ContactData, save_contact
from apps.clients.models import ClientCompany, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.models import Playbook, VersionState
from apps.cycles.services import playbooks
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.ui.factories import add_client, client_base

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
FEATURES: Final = ("cycles_r1", "import_r1")
LEAVE_PROMPT: Final = "beforeunload"
REFUSED: Final = "status of 422"
CONTACT_NAME: Final = "Dona Maria"
PLAYBOOK_NAME: Final = "Rotina MEI do escritório"


def _field(label: str) -> re.Pattern[str]:
    """Match a field's label exactly, with or without the required-field asterisk."""
    return re.compile(rf"^{re.escape(label)}( \*)?$")


def _client(live: LiveFirm) -> ClientCompany:
    slug = live.firm.tenant.slug
    return add_client(
        live.firm, legal_name="Padaria Guardada MEI", base=client_base(1, slug)
    )


def _follow(shell: BrowserPage, link: Locator) -> None:
    """Activate `link` as this viewport's user would: a tap, or Enter on focus."""
    if shell.viewport == "mobile":
        link.tap()
        return
    link.focus()
    expect(link).to_be_focused()
    shell.page.keyboard.press("Enter")


def _leave_but_stay(shell: BrowserPage, link: Locator) -> None:
    """Follow `link`, be asked, and answer "stay on this page"."""
    page = shell.page
    page.once("dialog", lambda dialog: dialog.dismiss())
    with page.expect_event("dialog") as asked:
        _follow(shell, link)
    assert asked.value.type == LEAVE_PROMPT


def _leave_confirmed(shell: BrowserPage, link: Locator) -> None:
    """Follow `link`, be asked, and answer "leave"."""
    page = shell.page
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_navigation(), page.expect_event("dialog") as asked:
        _follow(shell, link)
    assert asked.value.type == LEAVE_PROMPT


def _leave_unasked(shell: BrowserPage, link: Locator) -> None:
    """Follow `link` and arrive; any prompt on the way is recorded and fails."""
    page = shell.page
    prompts: list[str] = []

    def record(dialog: Dialog) -> None:
        prompts.append(dialog.type)
        dialog.accept()

    page.on("dialog", record)
    try:
        with page.expect_navigation():
            _follow(shell, link)
    finally:
        page.remove_listener("dialog", record)
    assert prompts == [], f"left a form with nothing to lose and was asked: {prompts}"


def _dashboard(live: LiveFirm) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(live.url(reverse('dashboard')))}(\?.*)?$")


def _console_beyond_refusals(shell: BrowserPage) -> list[str]:
    return [error for error in shell.console_errors if REFUSED not in error]


def test_a_refused_contact_stays_guarded_until_it_is_saved(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    """Server validation is the authority: its refusal leaves the edit at risk.

    The red witness for AXR1-040. Before it, the setup page carried no guard at
    all, and a guard that only learned of edits from keystrokes would start the
    refused re-render clean -- so the first prompt below never came.
    """
    live = live_firm("t40c", features=FEATURES)
    client = _client(live)
    shell = open_page(live, None)
    page = shell.page
    shell.goto(live.url(reverse("client-setup", args=[client.pk])))

    contatos = page.locator("#contatos")
    name = contatos.get_by_label(_field("Nome"))
    email = contatos.get_by_label(_field("E-mail"))
    name.fill(CONTACT_NAME)
    email.fill("dona.maria@")
    with page.expect_navigation():
        contatos.get_by_role("button", name="Adicionar contato").click()
    expect(email).to_have_attribute("aria-invalid", "true")
    expect(name).to_have_value(CONTACT_NAME)
    shell.screenshot("AXR1-040-refused-contact")

    refused_url = page.url
    back = page.get_by_role("link", name="Voltar para Clientes")
    _leave_but_stay(shell, back)
    expect(page).to_have_url(refused_url)
    expect(name).to_have_value(CONTACT_NAME)

    email.fill("dona.maria@padaria.example")
    with page.expect_navigation():
        contatos.get_by_role("button", name="Adicionar contato").click()
    expect(page.locator("#contatos tbody")).to_contain_text(CONTACT_NAME)
    expect(name).to_have_value("")

    _leave_unasked(shell, page.get_by_role("link", name="Voltar para Clientes"))
    expect(page).to_have_url(re.compile(r"/clientes/$"))
    assert_policed_roles()
    assert _console_beyond_refusals(shell) == []


def test_leaving_an_edited_contact_asks_and_staying_keeps_the_edit(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm("t40e", features=FEATURES)
    client = _client(live)
    with tenant_context(live.firm.tenant.id):
        contact = save_contact(
            actor=live.firm.owner,
            tenant_id=live.firm.tenant.id,
            client_id=client.pk,
            data=ContactData(name=CONTACT_NAME, email="dona.maria@padaria.example"),
        )
    shell = open_page(live, None)
    page = shell.page
    edit_url = live.url(reverse("client-contact-edit", args=[client.pk, contact.pk]))
    shell.goto(edit_url)

    phone = page.get_by_label(_field("Telefone"))
    phone.fill("(11) 98888-7777")
    cancel = page.get_by_role("link", name="Cancelar")

    _leave_but_stay(shell, cancel)
    expect(page).to_have_url(edit_url)
    expect(phone).to_have_value("(11) 98888-7777")
    shell.screenshot("AXR1-040-contact-kept")

    _leave_confirmed(shell, cancel)
    expect(page).to_have_url(re.compile(rf"/clientes/{client.pk}/cadastro/$"))
    with tenant_context(live.firm.tenant.id):
        assert ClientContact.objects.get(pk=contact.pk).phone == ""
    assert_policed_roles()
    shell.assert_clean_console()


def test_the_playbook_draft_asks_until_its_save_is_accepted(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm("t40p")
    with tenant_context(live.firm.tenant.id):
        system = Playbook.objects.get(key="mei-mensal")
        playbook = playbooks.clone_playbook(
            actor=live.firm.owner,
            tenant=live.firm.tenant,
            playbook_pk=system.pk,
        )
    shell = open_page(live, None)
    page = shell.page
    editor_url = live.url(reverse("playbook-editor", args=[playbook.pk, 1]))
    shell.goto(editor_url)

    name = page.get_by_label(_field("Nome do playbook"))
    step_label = page.locator("#id_steps-0-label_ptbr")
    home = page.get_by_role("link", name=settings.PRODUCT_NAME, exact=True)
    save = page.get_by_role("button", name="Salvar rascunho")

    name.fill(PLAYBOOK_NAME)
    _leave_but_stay(shell, home)
    expect(page).to_have_url(editor_url)
    expect(name).to_have_value(PLAYBOOK_NAME)

    step_label.fill("")
    with page.expect_navigation():
        save.click()
    expect(step_label).to_have_attribute("aria-invalid", "true")
    expect(name).to_have_value(PLAYBOOK_NAME)
    _leave_but_stay(shell, home)
    shell.screenshot("AXR1-040-playbook-refused")

    step_label.fill("Notas fiscais do mês")
    with page.expect_navigation():
        save.click()
    expect(page).to_have_url(editor_url)
    expect(name).to_have_value(PLAYBOOK_NAME)
    with tenant_context(live.firm.tenant.id):
        assert Playbook.objects.get(pk=playbook.pk).name == PLAYBOOK_NAME

    _leave_unasked(shell, home)
    expect(page).to_have_url(_dashboard(live))
    assert_policed_roles()
    assert _console_beyond_refusals(shell) == []


def test_pristine_and_read_only_forms_never_ask(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm("t40r", features=FEATURES)
    client = _client(live)
    with tenant_context(live.firm.tenant.id):
        system = Playbook.objects.get(key="mei-mensal")
        published = system.versions.get(state=VersionState.PUBLISHED)
    shell = open_page(live, None)
    page = shell.page

    shell.goto(live.url(reverse("client-setup", args=[client.pk])))
    _leave_unasked(shell, page.get_by_role("link", name="Voltar para Clientes"))
    expect(page).to_have_url(re.compile(r"/clientes/$"))

    # A published version cannot be saved, so typing into it loses nothing.
    shell.goto(live.url(reverse("playbook-editor", args=[system.pk, published.number])))
    expect(page.get_by_role("button", name="Salvar rascunho")).to_have_count(0)
    page.get_by_label(_field("Nome do playbook")).fill("Tentativa sem efeito")
    _leave_unasked(
        shell, page.get_by_role("link", name=settings.PRODUCT_NAME, exact=True)
    )
    expect(page).to_have_url(_dashboard(live))
    assert_policed_roles()
    shell.assert_clean_console()

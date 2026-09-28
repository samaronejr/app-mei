"""Onboarding a client in a real browser (AXR1-007): create -> profile -> invite.

Runs at 1440x900 and 390x844 on a firm carrying `cycles_r1` and `import_r1`, signed
in as its owner. Every navigation proves the live request ran as `app_runtime`, and
each screen is held to axe (serious/critical fail). The invitation mail is captured
by the locmem backend the test settings install; the live server shares this
process, so its sends land in `django.core.mail.outbox`.
"""

import re
from collections.abc import Callable
from datetime import date
from typing import Final

import pytest
from django.core import mail
from playwright.sync_api import FilePayload, expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany, ClientContact, TaxProfile
from apps.core.tenancy import tenant_context
from apps.delivery.tasks import dispatch_outbox
from apps.tenants.models import Invite
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.ui.factories import client_base, make_cnpj

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
FEATURES: Final = ("cycles_r1", "import_r1")
CONTACT_EMAIL: Final = "dona.maria@padaria.example"


def _field(label: str) -> re.Pattern[str]:
    """Match a field's label exactly, with or without the required-field asterisk."""
    return re.compile(rf"^{re.escape(label)}( \*)?$")


def _dispatch_queued_invitation(settings: SettingsWrapper) -> None:
    """T11: under cycles_r1 the invitation is queued on the outbox inside the request
    transaction and mailed by the dispatcher afterwards, never by the request."""
    assert mail.outbox == []
    settings.DELIVERY_PAUSED = False
    dispatch_outbox()


def test_create_a_client_set_its_profile_and_invite_a_contact(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
    settings: SettingsWrapper,
) -> None:
    live = live_firm("onb", features=FEATURES)
    shell = open_page(live, None)
    page = shell.page
    mail.outbox.clear()
    cnpj = make_cnpj(client_base(7, "onb"))

    # The registry offers the new entry points under the flag.
    shell.goto(live.url("/clientes/"))
    page.get_by_role("link", name="Novo cliente").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Novo cliente")
    axe_check(page)
    shell.screenshot("onboarding-create")

    page.get_by_label(_field("CNPJ")).fill(cnpj[:2] + "." + cnpj[2:5] + "." + cnpj[5:])
    page.get_by_label(_field("Razão social")).fill("Padaria da Dona Maria MEI")
    with page.expect_navigation():
        page.get_by_role("button", name="Cadastrar cliente").click()
    expect(page).to_have_url(re.compile(r"/clientes/[0-9a-f-]+/cadastro/$"))
    expect(page.get_by_role("heading", level=1)).to_have_text(
        "Padaria da Dona Maria MEI"
    )

    # Tax profile: MEI comum since March 2021 — history nobody had recorded yet.
    perfil = page.locator("#perfil")
    perfil.get_by_label(_field("Regime")).select_option(label="MEI")
    perfil.get_by_label(_field("Categoria MEI")).select_option(label="MEI comum")
    perfil.get_by_label(_field("Vale a partir de")).fill("2021-03-01")
    with page.expect_navigation():
        perfil.get_by_role("button", name="Registrar regime").click()
    expect(page.locator("#perfil tbody")).to_contain_text("MEI comum")
    expect(page.locator("#perfil tbody")).to_contain_text("Em vigor")

    # Contact with an e-mail address the client allowed.
    contatos = page.locator("#contatos")
    contatos.get_by_label(_field("Nome")).fill("Dona Maria")
    contatos.get_by_label(_field("E-mail")).fill(CONTACT_EMAIL)
    expect(contatos.get_by_label("Pode receber e-mails")).to_be_checked()
    with page.expect_navigation():
        contatos.get_by_role("button", name="Adicionar contato").click()
    invite = page.get_by_role("button", name="Convidar Dona Maria para o portal")
    expect(invite).to_be_visible()
    axe_check(page)

    with page.expect_navigation():
        invite.click()
    expect(page.locator("#contatos tbody")).to_contain_text("Convite pendente")
    axe_check(page)
    shell.assert_clean_console()
    shell.screenshot("onboarding-setup")

    _dispatch_queued_invitation(settings)
    assert [message.to for message in mail.outbox] == [[CONTACT_EMAIL]]
    assert "-portal." in mail.outbox[0].body
    tenant = live.firm.tenant
    with tenant_context(tenant.id):
        client = ClientCompany.objects.get(cnpj=cnpj)
        # The creation opened the timeline from today (the flag's claim); the
        # correction to 2021-03-01 superseded it, so exactly one claim is current.
        current = TaxProfile.objects.filter(client=client, superseded_at__isnull=True)
        assert list(current.values_list("regime", "valid_from", "valid_to")) == [
            ("mei", date(2021, 3, 1), None),
        ]
        assert ClientContact.objects.get(client=client).email == CONTACT_EMAIL
    invitation = Invite.objects.for_user(live.firm.owner).get(tenant=tenant)
    assert (invitation.email, invitation.client_id) == (CONTACT_EMAIL, client.pk)


def test_the_import_wizard_asks_for_columns_then_shows_the_dry_run(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = live_firm("imp", features=FEATURES)
    shell = open_page(live, None)
    page = shell.page
    good, dup_base = make_cnpj(client_base(1, "imp")), client_base(2, "imp")
    rows = [
        "Documento;Firma",
        f"{good};Mercearia Importada MEI",
        "12345678000100;=cmd|' /c calc'!A1",
        f"{make_cnpj(dup_base)};Repetida MEI",
        f"{make_cnpj(dup_base)};Repetida de novo MEI",
    ]
    payload = FilePayload(
        name="carteira.csv",
        mimeType="text/csv",
        buffer="\n".join(rows).encode(),
    )

    shell.goto(live.url("/clientes/"))
    page.get_by_role("link", name="Importar clientes").click()
    page.get_by_label(_field("Arquivo CSV ou XLSX")).set_input_files(payload)
    with page.expect_navigation():
        page.get_by_role("button", name="Gerar prévia").click()
    # Headers it does not know: the wizard asks, and says why (422, mapping state).
    expect(page.get_by_role("alert")).to_contain_text("Escolha a coluna de")
    axe_check(page)
    shell.screenshot("import-mapping")

    page.get_by_label(_field("CNPJ")).select_option(label="Documento")
    page.get_by_label(_field("Razão social")).select_option(label="Firma")
    page.get_by_label(_field("Arquivo CSV ou XLSX")).set_input_files(payload)
    with page.expect_navigation():
        page.get_by_role("button", name="Gerar prévia").click()

    expect(page.locator('[data-count="created"]')).to_have_text("2")
    expect(page.locator('[data-count="invalid"]')).to_have_text("1")
    expect(page.locator('[data-count="duplicate"]')).to_have_text("1")
    expect(page.get_by_role("button", name="Importar 2 clientes")).to_be_visible()
    expect(
        page.get_by_role("link", name="Baixar relatório de erros (CSV)")
    ).to_be_visible()
    axe_check(page)
    # The mapping step answered 422 by design (plan S7: ValidationFailed -> 422),
    # which Chromium logs as a failed resource. Anything else is a real error.
    unexpected = [e for e in shell.console_errors if "status of 422" not in e]
    assert unexpected == []
    shell.screenshot("import-plan")

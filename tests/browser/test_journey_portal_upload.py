"""The earliest customer journey (T11): the firm asks, the client uploads, "Recebido".

Mobile 390x844 on a throttled connection (Chrome DevTools network emulation): the
upload progress is visible, "Cancelar envio" aborts the request and keeps the chosen
file, "Tentar de novo" resends it, and the "Recebido" chip appears -- on the request
page and on Início. The staff side is exercised at 1440x900 on the cycle page.

No fixed sleep: every wait is Playwright's auto-waiting `expect` or a response wait.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from playwright.sync_api import CDPSession, FilePayload, expect

from apps.accounts.models import User
from apps.clients.models import ClientContact, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceLink, EvidenceRequest, PlaybookVersion
from apps.cycles.packs import install_system_packs
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.tenants.models import TenantRole
from tests.browser.harness import (
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
    axe_check,
    mfa_session_cookie,
)
from tests.ui.factories import add_client, add_member

pytestmark = pytest.mark.browser

PERIOD = date(2026, 9, 1)
KIB = 1024
# Big enough that a throttled upload is visibly in flight when "Cancelar" is pressed.
PAYLOAD = b"%PDF-1.4 journey\n" + b"0" * (1536 * KIB)
SLOW = {"latency": 150, "downloadThroughput": 256 * KIB, "uploadThroughput": 48 * KIB}
FAST = {"latency": 20, "downloadThroughput": 4096 * KIB, "uploadThroughput": 4096 * KIB}


@dataclass
class Journey:
    live: LiveFirm
    request: EvidenceRequest
    cycle_id: object
    portal_user: User


def _journey(live: LiveFirm) -> Journey:
    firm = live.firm
    company = add_client(firm, legal_name="Padaria Aurora MEI", base="112223330001")
    portal_user = add_member(
        firm.tenant, "dona@aurora.example.com", TenantRole.CLIENT_OWNER, client=company
    )
    with tenant_context(firm.tenant.pk):
        install_system_packs(firm.tenant)
        engagement = Engagement.objects.create(
            tenant=firm.tenant,
            client=company,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=firm.owner,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=firm.tenant,
            client=company,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            firm.owner, engagement.pk, PERIOD, definition.pk, idempotency_key="journey"
        )
        ClientContact.objects.create(
            tenant=firm.tenant,
            client=company,
            name="Dona Aurora",
            email=portal_user.email,
        )
        ClientContact.objects.create(
            tenant=firm.tenant,
            client=company,
            name="Contato sem acesso",
            email="semacesso@aurora.example.com",
        )
        request = create_request(
            firm.owner,
            ref.id,
            RequestFields(
                title="Notas de serviço de setembro",
                client_note=(
                    "Precisamos das notas de serviço emitidas em setembro para "
                    "concluir a apuração."
                ),
                due_on=date.today() + timedelta(days=5),  # noqa: DTZ011
                instructions="Conferir numeração sequencial.",
            ),
            expected_version=ref.version,
        )
        send_requests(firm.owner, ref.id, expected_version=ref.version + 1)
    return Journey(live, request, ref.id, portal_user)


def _throttle(page: BrowserPage, conditions: dict[str, int]) -> CDPSession:
    session = page.page.context.new_cdp_session(page.page)
    session.send("Network.enable")
    session.send("Network.emulateNetworkConditions", {"offline": False, **conditions})
    return session


@pytest.mark.parametrize("viewport", ["mobile"])
def test_portal_upload_progress_abort_retry_and_recebido(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    journey = _journey(live_firm("aurora"))
    live = journey.live
    portal = open_page(live, journey.portal_user)
    portal.page.context.add_cookies(
        [mfa_session_cookie(journey.portal_user, live.url("/", portal=True))]
    )
    portal.goto(live.url("/", portal=True))
    chip = portal.page.locator(f"[data-request-id='{journey.request.pk}'] .badge")
    expect(chip).to_have_text("Pendente")
    axe_check(portal.page)
    portal.screenshot("AXR1-011-portal-inicio-pendente")
    portal.page.get_by_role("link", name="Notas de serviço de setembro").click()
    expect(portal.page.locator("h1")).to_have_text("Notas de serviço de setembro")
    expect(portal.page.locator("#request-status .badge")).to_have_text("Pendente")

    cdp = _throttle(portal, SLOW)
    portal.page.set_input_files(
        "#arquivo",
        files=[
            FilePayload(
                name="notas-setembro.pdf", mimeType="application/pdf", buffer=PAYLOAD
            )
        ],
    )
    portal.page.get_by_role("button", name="Enviar arquivo").click()
    progress = portal.page.locator("progress.upload-progress")
    expect(progress).to_be_visible()
    expect(portal.page.get_by_text("Enviando…", exact=False).first).to_be_visible()
    expect(progress).not_to_have_attribute("value", "0", timeout=15000)
    portal.screenshot("AXR1-011-portal-enviando", mid_request=True)

    portal.page.get_by_role("button", name="Cancelar envio").click()
    expect(portal.page.locator("#arquivo-estado")).to_contain_text("Envio cancelado")
    retry = portal.page.get_by_role("button", name="Tentar de novo")
    expect(retry).to_be_visible()
    # The file list survives the abort: the input still holds the chosen file.
    assert portal.page.locator("#arquivo").evaluate("input => input.files.length") == 1
    with tenant_context(live.firm.tenant.pk):
        assert not EvidenceLink.objects.filter(request=journey.request).exists()
    portal.screenshot("AXR1-011-portal-cancelado")
    # htmx reports an aborted request on the console by design (its error-event
    # logging). Exactly those two entries, caused by the cancel just pressed, are
    # expected here; anything else -- a CSP refusal, an uncaught error -- still fails.
    assert portal.console_errors == [
        "htmx:afterRequest, [object HTMLElement]",
        "htmx:sendAbort, [object HTMLElement]",
    ], portal.console_errors
    portal.console_errors.clear()

    cdp.send("Network.emulateNetworkConditions", {"offline": False, **FAST})
    with portal.page.expect_response(
        lambda response: (
            response.url.endswith(f"/solicitacoes/{journey.request.pk}/enviar")
            and response.request.method == "POST"
        ),
        timeout=30000,
    ) as sent:
        retry.click()
    assert sent.value.status == 200
    status_chip = portal.page.locator("#request-status .badge")
    expect(status_chip).to_have_text("Recebido")
    assert "badge-ok" not in (status_chip.get_attribute("class") or "")
    expect(portal.page.locator("#request-status")).to_contain_text("notas-setembro.pdf")
    expect(portal.page.locator("#arquivo-estado")).to_contain_text("Arquivo enviado")
    axe_check(portal.page)
    portal.screenshot("AXR1-011-portal-recebido")
    with tenant_context(live.firm.tenant.pk):
        assert EvidenceLink.objects.filter(request=journey.request).count() == 1

    portal.goto(live.url("/", portal=True))
    expect(chip).to_have_text("Recebido")
    expect(portal.page.locator("main")).not_to_contain_text("numeração sequencial")
    axe_check(portal.page)
    portal.screenshot("AXR1-011-portal-inicio-recebido")
    portal.assert_clean_console()
    assert_policed_roles()


@pytest.mark.parametrize("viewport", ["desktop"])
def test_staff_sees_requests_preview_and_recebido(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    journey = _journey(live_firm("aurora-staff"))
    live = journey.live
    from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: PLC0415

    from apps.cycles.intake import intake_upload  # noqa: PLC0415

    with tenant_context(live.firm.tenant.pk):
        company = EvidenceRequest.objects.get(pk=journey.request.pk).client
        intake_upload(
            journey.portal_user,
            journey.request.pk,
            SimpleUploadedFile("notas-setembro.pdf", b"%PDF-1.4 staff view\n"),
            tenant=live.firm.tenant,
            client=company,
        )
    page = open_page(live, None)
    page.goto(live.url(f"/ciclos/{journey.cycle_id}/"))
    section = page.page.locator("#solicitacoes")
    expect(section).to_contain_text("Notas de serviço de setembro")
    expect(section).to_contain_text("Recebido do cliente · aguardando conferência")
    expect(section).to_contain_text("notas-setembro.pdf")
    expect(section.locator("[data-access='portal']")).to_contain_text("Dona Aurora")
    expect(section.locator("[data-access='ineligible']")).to_contain_text(
        "não receberá"
    )
    page.page.locator("#new-title").fill("Extrato bancário de setembro")
    with page.page.expect_response(
        lambda response: (
            "/solicitacoes/" in response.url and response.request.method == "POST"
        ),
        timeout=10000,
    ) as created:
        page.page.get_by_role("button", name="Adicionar solicitação").click()
    assert created.value.status == 302
    expect(section).to_contain_text("Extrato bancário de setembro")
    axe_check(page.page)
    page.screenshot("AXR1-011-staff-solicitacoes")
    page.assert_clean_console()
    assert_policed_roles()

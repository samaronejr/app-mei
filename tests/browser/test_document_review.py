"""T13 review workspace in a real browser: keyboard-only reject and accept.

Two client uploads are scanned (FakeScanner at the port) and extracted by the real
service. The reviewer opens the queue, then the first document, and decides both with
the keyboard alone: R focuses the reason, Tab reaches "Não aceitar", Enter submits;
the page moves to the next document, where A accepts. The client then reads the
reason in the portal. Runs at 1440x900 and 390x844; waits are Playwright's own.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientContact, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    RequestState,
    ReviewDecision,
)
from apps.cycles.extraction.service import extract_intake
from apps.cycles.intake import intake_upload
from apps.cycles.models import PlaybookVersion
from apps.cycles.packs import install_system_packs
from apps.cycles.scanning import FakeScanner, scan_intake
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
from tests.cycles.document_fixtures import fixture
from tests.ui.factories import add_client, add_member

pytestmark = pytest.mark.browser

PERIOD = date(2026, 9, 1)
REASON = "O arquivo é de outra empresa. Envie a nota emitida pelo seu CNPJ."


@dataclass
class Desk:
    live: LiveFirm
    request: EvidenceRequest
    portal_user: User
    rejected: EvidenceLink
    accepted: EvidenceLink


def _desk(live: LiveFirm) -> Desk:
    firm = live.firm
    company = add_client(firm, legal_name="Ateliê Sintético MEI", base="112223330001")
    portal_user = add_member(
        firm.tenant, "dona@atelie.example.com", TenantRole.CLIENT_OWNER, client=company
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
            firm.owner, engagement.pk, PERIOD, definition.pk, idempotency_key="t13"
        )
        ClientContact.objects.create(
            tenant=firm.tenant, client=company, name="Dona", email=portal_user.email
        )
        request = create_request(
            firm.owner,
            ref.id,
            RequestFields(
                title="Notas fiscais de setembro",
                client_note="Envie as notas emitidas em setembro.",
                due_on=timezone.localdate() + timedelta(days=5),
            ),
            expected_version=ref.version,
        )
        send_requests(firm.owner, ref.id, expected_version=ref.version + 1)
        for name in ("nfe_outra_empresa.xml", "nfe_emitente_cliente.xml"):
            intake_upload(
                portal_user,
                request.pk,
                SimpleUploadedFile(name, fixture(name), "application/xml"),
                tenant=firm.tenant,
                client=company,
            )
        intakes = list(DocumentIntake.objects.order_by("created_at", "pk"))
    for intake in intakes:
        scan_intake(
            firm.tenant.pk, intake.pk, scanner=FakeScanner(), now=timezone.now()
        )
        extract_intake(firm.tenant.pk, intake.pk, now=timezone.now())
    with tenant_context(firm.tenant.pk):
        links = list(EvidenceLink.objects.order_by("created_at", "pk"))
    return Desk(live, request, portal_user, links[0], links[1])


def test_keyboard_only_reject_then_accept_and_the_client_reads_the_reason(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    desk = _desk(live_firm("atelie"))
    live = desk.live
    page = open_page(live, None)

    page.goto(live.url("/documentos/?fila=divergentes"))
    row = page.page.locator(f"[data-link-id='{desk.rejected.pk}']")
    expect(row).to_contain_text("Outra empresa: CNPJ divergente")
    expect(page.page.locator("nav.shell-nav")).to_contain_text("Documentos")
    axe_check(page.page)
    page.screenshot("AXR1-013-fila-documentos")

    page.goto(live.url(f"/documentos/{desk.rejected.pk}/revisar/"))
    expect(page.page.locator("[data-note='cnpj_divergente']")).to_be_visible()
    expect(
        page.page.locator("[data-field='emitente_cnpj'] [data-suggested]")
    ).to_have_text("11444777000161")
    assert (viewport := page.page.viewport_size) is not None
    assert (
        page.page.evaluate("document.documentElement.scrollWidth") <= viewport["width"]
    )
    axe_check(page.page)
    page.screenshot("AXR1-013-revisao")

    # Keyboard only: R focuses the reason, Tab reaches "Não aceitar", Enter submits.
    page.page.locator("body").focus()
    page.page.keyboard.press("r")
    reason = page.page.locator("#client_reason")
    expect(reason).to_be_focused()
    page.page.keyboard.type(REASON)
    page.page.keyboard.press("Tab")
    expect(page.page.get_by_role("button", name="Não aceitar")).to_be_focused()
    with (
        page.page.expect_event("load", timeout=10000),
        page.page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/revisar/" in response.url
            ),
            timeout=10000,
        ) as rejected,
    ):
        page.page.keyboard.press("Enter")
    assert rejected.value.status == 302
    # The next undecided document opens; A accepts it.
    expect(page.page).to_have_url(live.url(f"/documentos/{desk.accepted.pk}/revisar/"))
    with (
        page.page.expect_event("load", timeout=10000),
        page.page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/revisar/" in response.url
            ),
            timeout=10000,
        ) as accepted,
    ):
        page.page.keyboard.press("a")
    assert accepted.value.status == 302
    expect(page.page).to_have_url(live.url("/documentos/"))
    page.assert_clean_console()

    with tenant_context(live.firm.tenant.pk):
        desk.rejected.refresh_from_db()
        desk.accepted.refresh_from_db()
        assert desk.rejected.review_decision == ReviewDecision.REJECTED
        assert desk.rejected.client_reason == REASON
        assert desk.accepted.review_decision == ReviewDecision.ACCEPTED
        assert (
            EvidenceRequest.objects.get(pk=desk.request.pk).state
            == RequestState.ACCEPTED
        )

    portal = open_page(live, desk.portal_user)
    portal.page.context.add_cookies(
        [mfa_session_cookie(desk.portal_user, live.url("/", portal=True))]
    )
    portal.goto(live.url(f"/solicitacoes/{desk.request.pk}/", portal=True))
    declined = portal.page.locator("#arquivos-nao-aceitos")
    expect(declined).to_contain_text("nfe_outra_empresa.xml")
    expect(declined.locator("[data-client-reason]")).to_have_text(REASON)
    axe_check(portal.page)
    portal.screenshot("AXR1-013-portal-motivo")
    portal.assert_clean_console()
    assert_policed_roles()

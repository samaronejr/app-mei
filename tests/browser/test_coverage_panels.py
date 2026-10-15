"""T17 panels: the Cobertura section and the receipt request surface."""

from collections.abc import Callable

import pytest
from django.test import Client as DjangoTestClient
from playwright._impl._api_structures import SetCookieParam

from apps.accounts.models import User
from apps.clients.models import (
    ClientCompany,
    ClientContact,
    Engagement,
    TaxProfile,
)
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceRequest
from apps.cycles.models import PlaybookVersion
from apps.cycles.packs import install_system_packs
from apps.cycles.records import ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models.operations import Obligation
from apps.obligations.observations import record_staff_fact
from apps.tenants.models import TenantRole
from tests.browser.harness import BrowserPage, LiveFirm, axe_check
from tests.cycles.cycle_fixtures import PERIOD
from tests.ui.factories import add_client, add_member

pytestmark = pytest.mark.browser

CYCLES_FLAG = "cycles_r1"
OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def _open_cycle_world(
    live: LiveFirm,
) -> tuple[ClientCompany, ServiceCycle, EvidenceRequest, User]:
    client = add_client(
        live.firm, legal_name="Padaria Leste Sintética", base="112223330001"
    )
    with tenant_context(live.firm.tenant.pk):
        install_system_packs(live.firm.tenant)
        engagement = Engagement.objects.create(
            tenant=live.firm.tenant,
            client=client,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=live.firm.owner,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=live.firm.tenant,
            client=client,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        generate_das_calendar(client, starting_from=PERIOD)
        obligation = Obligation.objects.get(
            client=client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
        )
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            live.firm.owner,
            engagement.pk,
            PERIOD,
            definition.pk,
            idempotency_key="t17-browser",
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        receipt = create_request(
            live.firm.owner,
            cycle.pk,
            RequestFields(
                title="Comprovante de pagamento do DAS",
                kind="comprovante",
                obligation_id=obligation.pk,
            ),
            expected_version=cycle.version,
        )
        portal_user_pre = add_member(
            live.firm.tenant,
            "portal@t17-browser.test",
            TenantRole.CLIENT_OWNER,
            client=client,
        )
        ClientContact.objects.create(
            tenant=live.firm.tenant,
            client=client,
            name="Contato Portal",
            email=portal_user_pre.email,
        )
        cycle.refresh_from_db()
        send_requests(live.firm.owner, cycle.pk, expected_version=cycle.version)
        record_staff_fact(
            live.firm.owner,
            subject_id=obligation.pk,
            kind="payment",
            period=PERIOD,
            source_ref="DAS 09/2026",
        )
    return client, cycle, receipt, portal_user_pre


def _portal_context_cookie(
    live: LiveFirm, portal_user: User, client: ClientCompany
) -> SetCookieParam:
    session_client = DjangoTestClient()
    session_client.force_login(portal_user)
    session = session_client.session
    session["portal_client_id"] = str(client.pk)
    session.save()
    return {
        "name": "sessionid",
        "value": str(session.session_key),
        "url": live.url("/", portal=True),
    }


def test_workspace_coverage_panel(
    open_page: OpenPage,
    live_firm: Callable[..., LiveFirm],
    viewport: str,
) -> None:
    live = live_firm(features=(CYCLES_FLAG,))
    client, _cycle, _receipt, _portal = _open_cycle_world(live)
    page = open_page(live, None)
    page.goto(live.url(f"/clientes/{client.pk}/?periodo={PERIOD:%Y-%m}"))
    coverage = page.page.locator("#cobertura")
    coverage.scroll_into_view_if_needed()
    assert coverage.locator("[data-coverage-state='manual']").count() == 1
    assert "Pagamento informado" in coverage.inner_text()
    page.screenshot("AXR1-017-workspace-cobertura")
    page.assert_clean_console()
    assert not axe_check(page.page), "axe serious/critical violation"


def test_cycle_detail_coverage_and_receipt_form(
    open_page: OpenPage,
    live_firm: Callable[..., LiveFirm],
    viewport: str,
) -> None:
    live = live_firm(features=(CYCLES_FLAG,))
    _client, cycle, _receipt, _portal = _open_cycle_world(live)
    page = open_page(live, None)
    page.goto(live.url(f"/ciclos/{cycle.pk}/"))
    coverage = page.page.locator("#cobertura")
    assert coverage.locator("[data-coverage-state='manual']").count() == 1
    assert page.page.locator("[data-request-kind='comprovante']").count() == 1
    assert page.page.locator("#new-kind").count() == 1
    coverage.scroll_into_view_if_needed()
    page.screenshot("AXR1-017-cycle-cobertura")
    page.assert_clean_console()
    assert not axe_check(page.page)


def test_portal_request_shows_receipt_hint(
    open_page: OpenPage,
    live_firm: Callable[..., LiveFirm],
    viewport: str,
) -> None:
    live = live_firm(features=(CYCLES_FLAG,))
    client, _cycle, receipt, portal_user = _open_cycle_world(live)
    page = open_page(live, portal_user)
    page.page.context.add_cookies([_portal_context_cookie(live, portal_user, client)])
    page.goto(live.url(f"/solicitacoes/{receipt.pk}/", portal=True))
    assert page.page.locator("[data-request-kind='comprovante']").count() == 1
    assert "Comprovante de pagamento" in page.page.inner_text("body")
    page.screenshot("AXR1-017-portal-receipt-request")
    page.assert_clean_console()
    assert not axe_check(page.page)


def test_portal_payments_informed_badge(
    open_page: OpenPage,
    live_firm: Callable[..., LiveFirm],
    viewport: str,
) -> None:
    live = live_firm(features=(CYCLES_FLAG,))
    client, _cycle, _receipt, portal_user = _open_cycle_world(live)
    page = open_page(live, portal_user)
    page.page.context.add_cookies([_portal_context_cookie(live, portal_user, client)])
    page.goto(live.url("/pagamentos/", portal=True))
    assert page.page.locator("[data-payment-state='manual']").count() >= 1
    page.screenshot("AXR1-017-portal-payments")
    page.assert_clean_console()
    assert not axe_check(page.page)

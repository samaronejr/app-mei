"""T14: the drawer opens and acts by keyboard; the portal sees only its sentence."""

from collections.abc import Callable
from datetime import date
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.clients.models import ClientCompany, Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ExceptionState, PlaybookVersion, ServiceCycle
from apps.cycles.openers import CERTIFICATE_EXPIRING
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.exceptions import open_exception
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


def _cycle_with_exceptions(live: LiveFirm) -> tuple[ClientCompany, ServiceCycle]:
    client = add_client(
        live.firm, legal_name="Oficina Gama Sintética", base="112223330001"
    )
    with tenant_context(live.firm.tenant.pk):
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
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        ref = open_cycle(
            live.firm.owner,
            engagement.pk,
            PERIOD,
            definition.pk,
            idempotency_key="t14-browser",
        )
        for version, (code, subject) in enumerate(
            ((CERTIFICATE_EXPIRING, client.pk), ("delivery_unknown", uuid4())),
            start=ref.version,
        ):
            open_exception(
                code,
                subject,
                actor=live.firm.owner,
                cycle_id=ref.id,
                expected_version=version,
                owner=live.firm.owner,
                review_on=PERIOD,
            )
        return client, ServiceCycle.objects.get(pk=ref.id)


def test_exception_drawer_is_keyboard_operable_and_portal_sees_only_its_sentence(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm()
    client, cycle = _cycle_with_exceptions(live)
    page = open_page(live, None)
    page.goto(live.url(f"/ciclos/{cycle.pk}/"))

    section = page.page.locator("#pendencias")
    drawers = section.locator("details[data-exception-id]")
    expect(drawers).to_have_count(2)
    uncertain = section.locator("details[data-reason='delivery_unknown']")
    summary = uncertain.locator("summary")
    summary.focus()
    expect(summary).to_be_focused()
    page.page.keyboard.press("Enter")
    expect(uncertain).to_have_attribute("open", "")
    start = uncertain.get_by_role("button", name="Começar a tratar", exact=True)
    start.focus()
    with page.page.expect_response(
        lambda response: (
            "/ciclos/pendencias/" in response.url and response.request.method == "POST"
        ),
        timeout=10000,
    ) as posting:
        page.page.keyboard.press("Enter")
    assert posting.value.status == 302

    uncertain = page.page.locator("#pendencias details[data-reason='delivery_unknown']")
    uncertain.locator("summary").focus()
    page.page.keyboard.press("Enter")
    expect(uncertain).to_contain_text("Em tratamento")
    expect(uncertain).to_contain_text("não pode ser aceita como risco")
    expect(
        uncertain.get_by_role("button", name="Aceitar como risco", exact=True)
    ).to_have_count(0)
    expect(uncertain.get_by_label("O que foi feito ou motivo")).to_be_visible()
    axe_check(page.page)
    page.screenshot("AXR1-014-drawer")
    page.assert_clean_console()
    with tenant_context(live.firm.tenant.pk):
        assert (
            CycleException.objects.get(reason_code="delivery_unknown").state
            == ExceptionState.HANDLING
        )

    portal_user = add_member(
        live.firm.tenant,
        "dona@gama.example.com",
        TenantRole.CLIENT_OWNER,
        client=client,
    )
    portal = open_page(live, portal_user)
    portal.page.context.add_cookies(
        [mfa_session_cookie(portal_user, live.url("/", portal=True))]
    )
    portal.goto(live.url("/", portal=True))
    main = portal.page.locator("main")
    expect(main).to_contain_text("Precisamos da sua ajuda")
    expect(main).to_contain_text("Seu certificado digital vence em breve")
    expect(main).not_to_contain_text("Verifique o envio antes de decidir")
    expect(main).not_to_contain_text("Avise o cliente")
    axe_check(portal.page)
    portal.screenshot("AXR1-014-portal-help")
    portal.assert_clean_console()
    assert_policed_roles()

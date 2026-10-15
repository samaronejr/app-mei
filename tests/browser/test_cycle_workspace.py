"""The real workspace opens, transitions and filters cycles at both viewports."""

from collections.abc import Callable
from datetime import date

import pytest
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.clients.models import Engagement, TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import add_note
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


def test_workspace_and_cycle_list(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm()
    company = add_client(
        live.firm, legal_name="Cliente sintético de ciclos", base="112223330001"
    )
    period = date(2026, 9, 1)
    with tenant_context(live.firm.tenant.pk):
        Engagement.objects.create(
            tenant=live.firm.tenant,
            client=company,
            service_kind="mei_mensal",
            starts_on=period,
            responsible=live.firm.owner,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=live.firm.tenant,
            client=company,
            regime="mei",
            valid_from=period,
            provenance="staff_entry",
        )
    page = open_page(live, None)
    page.goto(live.url(f"/clientes/{company.pk}/?periodo=2026-09"))
    with page.page.expect_response(
        lambda response: (
            "/ciclos/abrir/" in response.url and response.request.method == "POST"
        ),
        timeout=10000,
    ) as opening:
        page.page.get_by_role("button", name="Abrir ciclo", exact=True).click()
    assert opening.value.status == 302
    expect(page.page.locator("input[name='expected_version']").first).to_have_value("1")
    page.page.locator("#target").select_option("em_andamento")
    with page.page.expect_response(
        lambda response: (
            "/ciclos/" in response.url and response.request.method == "POST"
        ),
        timeout=10000,
    ) as transition:
        page.page.get_by_role("button", name="Alterar estado", exact=True).click()
    assert transition.value.status == 302
    expect(page.page.locator("input[name='expected_version']").first).to_have_value("2")
    axe_check(page.page)
    page.screenshot("AXR1-009-cycle-detail")
    with tenant_context(live.firm.tenant.pk):
        cycle = ServiceCycle.objects.get(client=company)
        assert cycle.state == "em_andamento"
        assert CycleEvent.objects.filter(cycle=cycle).count() == 2
    page.goto(live.url("/ciclos/?periodo=2026-09&estado=em_andamento"))
    expect(page.page.locator(f"a[href='/ciclos/{cycle.pk}/']")).to_be_visible()
    axe_check(page.page)
    page.screenshot("AXR1-009-ciclos")
    page.goto(live.url(f"/clientes/{company.pk}/?periodo=2026-09"))
    expect(page.page.locator(f"a[href='/ciclos/{cycle.pk}/']")).to_be_visible()
    axe_check(page.page)
    page.screenshot("AXR1-009-workspace")
    assert_policed_roles()
    page.assert_clean_console()
    with tenant_context(live.firm.tenant.pk):
        add_note(live.firm.owner, cycle.pk, "PRIVATE-CYCLE-CANARY", expected_version=2)
    portal_user = add_member(
        live.firm.tenant,
        "portal@acme.example.com",
        TenantRole.CLIENT_OWNER,
        client=company,
    )
    portal = open_page(live, portal_user)
    portal.page.context.add_cookies(
        [mfa_session_cookie(portal_user, live.url("/", portal=True))]
    )
    portal.goto(live.url("/", portal=True))
    link = portal.page.locator(f"a[href='/ciclos/{cycle.pk}/']")
    expect(link).to_be_visible()
    link.click()
    expect(portal.page.locator("#cycle-period")).to_be_visible()
    expect(portal.page.locator("main")).not_to_contain_text("PRIVATE-CYCLE-CANARY")
    axe_check(portal.page)
    portal.screenshot("AXR1-009-portal")
    portal.assert_clean_console()
    assert_policed_roles()

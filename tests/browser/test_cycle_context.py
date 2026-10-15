"""Two clients sharing one period/playbook remain identifiable before any write."""

from collections.abc import Callable
from datetime import date

import pytest
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.clients.models import Engagement, TaxProfile
from apps.core.templatetags.ptbr import cnpj_mask
from apps.core.tenancy import tenant_context
from apps.cycles.forms import SERVICE_KIND_CHOICES
from apps.cycles.models import PlaybookVersion, ServiceCycle
from apps.cycles.services.cycle import open_cycle
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles, axe_check
from tests.ui.factories import add_client

pytestmark = pytest.mark.browser


def test_cycle_header_identifies_client_engagement_period_and_state(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm()
    clients = [
        add_client(live.firm, legal_name="Oficina Alfa Sintética", base="112223330001"),
        add_client(live.firm, legal_name="Loja Beta Sintética", base="114447770001"),
    ]
    period = date(2026, 9, 1)
    cycles = []
    with tenant_context(live.firm.tenant.pk):
        definition = PlaybookVersion.objects.get(
            playbook__key="mei-mensal", state="published"
        )
        for client in clients:
            engagement = Engagement.objects.create(
                tenant=live.firm.tenant,
                client=client,
                service_kind="mei_mensal",
                starts_on=period,
                responsible=live.firm.owner,
                provenance="staff_entry",
            )
            TaxProfile.objects.create(
                tenant=live.firm.tenant,
                client=client,
                regime="mei",
                valid_from=period,
                provenance="staff_entry",
            )
            ref = open_cycle(
                live.firm.owner,
                engagement.pk,
                period,
                definition.pk,
                idempotency_key=str(client.pk),
            )
            cycles.append(
                ServiceCycle.objects.select_related("engagement").get(pk=ref.id)
            )
    page = open_page(live, None)
    for index, (client, cycle) in enumerate(zip(clients, cycles, strict=True)):
        page.goto(live.url(f"/ciclos/{cycle.pk}/"))
        header = page.page.locator("#cycle-context")
        expect(header).to_be_visible()
        expect(header).to_contain_text(client.legal_name)
        expect(header).to_contain_text(cnpj_mask(client.cnpj))
        expect(header).to_contain_text(
            dict(SERVICE_KIND_CHOICES)[cycle.engagement.service_kind]
        )
        expect(header).to_contain_text(period.strftime("%m/%Y"))
        expect(header).to_contain_text(cycle.get_state_display())
        expect(header.locator("[data-engagement-id]")).to_have_attribute(
            "data-engagement-id", str(cycle.engagement_id)
        )
        other = clients[1 - index]
        expect(header).not_to_contain_text(other.legal_name)
        expect(header).not_to_contain_text(cnpj_mask(other.cnpj))
        expect(page.page.locator("input[name='expected_version']").first).to_have_value(
            "1"
        )
        axe_check(page.page)
        page.screenshot(f"AXR1-009-fix-3-client-{index + 1}")
    page.assert_clean_console()
    assert_policed_roles()

"""The queue has observable pending, failed-refresh and forbidden states."""

from collections.abc import Callable

import pytest
from playwright.sync_api import Route, expect

from apps.accounts.models import User
from apps.authz.models import GrantLevel, RoleGrant
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus
from apps.tenants.models import TenantRole
from tests.briefing.fixtures import NOW
from tests.browser.harness import (
    BrowserPage,
    LiveFirm,
    assert_policed_roles,
    axe_check,
)
from tests.ui.factories import add_client

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
URL = "/?escopo=team&periodo=2026-09"


def _live_queue(live_firm: Callable[..., LiveFirm]) -> LiveFirm:
    live = live_firm(features=("cycles_r1", "briefing_r1"))
    client = add_client(live.firm, legal_name="Cliente da fila", base="112223330001")
    with tenant_context(live.firm.tenant.pk):
        Obligation.objects.create(
            tenant=live.firm.tenant,
            client=client,
            obligation_type_id="DAS",
            competence_month=NOW.date().replace(day=1),
            nominal_due_date=NOW.date(),
            resolved_due_date=NOW.date(),
            status=ObligationStatus.DUE,
        )
    return live


def test_refresh_preserves_timestamped_data_on_failure_and_can_retry(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = _live_queue(live_firm)
    opened = open_page(live, None)
    page = opened.page
    if opened.viewport == "mobile":
        page.emulate_media(reduced_motion="reduce")
    opened.goto(live.url(URL))
    expect(page.locator("[data-briefing-total]")).to_have_attribute(
        "data-briefing-total", "1"
    )
    computed_at = page.locator("#briefing-computed-at").get_attribute("datetime")
    pending: list[Route] = []

    def hold_refresh(route: Route) -> None:
        pending.append(route)
        page.evaluate("console.info('t19-refresh-paused')")

    page.route("**/?*", hold_refresh)
    page.locator("#briefing-filters").click()
    more_filters = page.locator("form[hx-get] details > summary")
    more_filters.click()
    expect(page.locator('form[hx-get] input[name="municipio"]')).to_be_visible()
    opened.screenshot("AXR1-019-filters")
    more_filters.click()
    submit = page.get_by_role("button", name="Aplicar filtros")
    with page.expect_console_message(
        predicate=lambda message: message.text == "t19-refresh-paused"
    ):
        submit.click()

    expect(page.locator("#briefing-loading")).to_be_visible()
    expect(page.locator("[data-briefing-skeleton]")).to_have_count(3)
    expect(page.locator("#briefing-results")).to_be_hidden()
    expect(submit).to_be_disabled()
    opened.screenshot("AXR1-019-loading", mid_request=True)

    with page.expect_response(lambda response: response.status == 503):
        pending.pop().fulfill(status=503, content_type="text/plain", body="Unavailable")

    stale = page.locator("#briefing-stale")
    expect(stale).to_be_visible()
    assert computed_at is not None
    expect(stale.locator("time")).to_have_attribute("datetime", computed_at)
    expect(page.locator("#briefing-loading")).to_be_hidden()
    expect(page.locator("[data-briefing-total]")).to_have_attribute(
        "data-briefing-total", "1"
    )
    expect(page.locator("[data-briefing-item]")).to_have_count(1)
    expect(submit).to_be_enabled()
    expect(page.locator("#htmx-error-fallback")).to_be_hidden()
    axe_check(page)
    opened.screenshot("AXR1-019-stale")

    page.unroute("**/?*", hold_refresh)
    with page.expect_response(
        lambda response: (
            response.status == 200
            and response.request.headers.get("hx-request") == "true"
        )
    ):
        submit.click()
    expect(page.locator("#briefing-stale")).to_be_hidden()
    expect(page.locator("#briefing-results")).to_be_visible()
    expect(page.locator("[data-briefing-total]")).to_have_attribute(
        "data-briefing-total", "1"
    )
    assert_policed_roles()
    assert all("503" in error for error in opened.console_errors), "\n".join(
        opened.console_errors
    )


def test_permission_revocation_does_not_leave_stale_counts_visible(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    live = _live_queue(live_firm)
    opened = open_page(live, None)
    page = opened.page
    opened.goto(live.url(URL))
    expect(page.locator("[data-briefing-total]")).to_have_attribute(
        "data-briefing-total", "1"
    )
    with tenant_context(live.firm.tenant.pk):
        RoleGrant.objects.filter(
            role=TenantRole.OWNER, capability__slug="clients.view_assigned"
        ).update(level=GrantLevel.NONE)

    page.locator("#briefing-filters").click()
    with page.expect_navigation(wait_until="domcontentloaded") as denied:
        page.get_by_role("button", name="Aplicar filtros").click()

    assert denied.value is not None
    assert denied.value.status == 403
    expect(page.locator("[data-briefing-total]")).to_have_count(0)
    expect(page.locator("[data-briefing-item]")).to_have_count(0)
    assert_policed_roles()
    opened.screenshot("AXR1-019-forbidden")

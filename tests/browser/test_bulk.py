"""The bulk rehearsal and per-item result flow on both real browser viewports."""

from collections.abc import Callable

import pytest
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItem, BulkKind, BulkOperation
from apps.cycles.models import ServiceCycle
from apps.cycles.tasks import run_bulk_operation
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles, axe_check
from tests.cycles.bulk_fixtures import (
    BulkSetup,
    bulk_setup,
    notice_preview_with_reservations,
    reserve_today,
)
from tests.support import enrol_totp
from tests.ui.factories import Firm, make_firm

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


@pytest.fixture
def bulk_desk(browser_server: LiveServer) -> tuple[BulkSetup, LiveFirm]:
    setup = bulk_setup("bulk-browser")
    enrol_totp(setup.graph.actor)
    firm = Firm(
        tenant=setup.graph.tenant,
        owner=setup.graph.actor,
        accountant=setup.graph.staff,
        clients=[setup.graph.client],
    )
    return setup, LiveFirm(firm=firm, port=browser_server.thread.port)


def test_count_exclusion_and_committed_results_are_visible_and_accessible(
    bulk_desk: tuple[BulkSetup, LiveFirm], open_page: OpenPage
) -> None:
    setup, live = bulk_desk
    opened = open_page(live, setup.graph.actor)
    page = opened.page
    opened.goto(live.url("/ciclos/lotes/"))
    opened.screenshot("t20-start")
    page.locator('select[name="kind"]').select_option(BulkKind.OPEN_CYCLES)
    page.locator('input[name="periodo"][type="month"]').fill("2026-09")
    page.locator('select[name="playbook_version_id"]').select_option(
        str(setup.graph.definition.pk)
    )
    page.locator('button[value="select"]').click()
    for engagement in setup.engagements:
        page.locator(f'[name="subject_ids"][value="{engagement.pk}"]').check()
    page.locator('button[value="preview"]').click()
    expect(page.locator("[data-bulk-preview]")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    opened.screenshot("t20-preview")
    page.locator(f'[name="exclusions"][value="{setup.engagements[2].pk}"]').check()
    page.locator('[name="confirm_count"]').fill("1")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and "/ciclos/lotes/" in response.url
        )
    ) as refused:
        page.locator('button[value="confirm"]').click()
    assert refused.value.status == 422
    expect(page.get_by_role("alert")).to_be_visible()
    with tenant_context(setup.graph.tenant.pk):
        assert not BulkItem.objects.exists()
    # The exclusion survives the validation error rather than silently broadening.
    expect(
        page.locator(f'[name="exclusions"][value="{setup.engagements[2].pk}"]')
    ).to_be_checked()
    page.locator('[name="confirm_count"]').fill("2")
    page.locator('button[value="confirm"]').focus()
    page.keyboard.press("Enter")
    expect(page.locator("[data-bulk-state]")).to_have_attribute(
        "data-bulk-state", "queued"
    )
    with tenant_context(setup.graph.tenant.pk):
        operation = BulkOperation.objects.get()
    cancellation = page.locator("#bulk-cancel")
    with page.expect_response(lambda response: response.url.endswith("/progresso/")):
        cancellation.focus()
    expect(cancellation).to_be_focused()
    page.locator('a[href="/ciclos/lotes/"]').focus()
    with page.expect_response(
        lambda response: (
            response.url.endswith("/progresso/") and response.request.method == "GET"
        )
    ):
        run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    expect(page.locator("[data-bulk-state]")).to_have_attribute(
        "data-bulk-state", "completed"
    )
    expect(page.locator('[hx-trigger="every 2s"]')).to_have_count(0)
    expect(page.locator('a[href="/ciclos/lotes/"]')).to_be_focused()
    with tenant_context(setup.graph.tenant.pk):
        assert set(ServiceCycle.objects.values_list("engagement_id", flat=True)) == {
            setup.engagements[0].pk,
            setup.engagements[1].pk,
        }
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    opened.screenshot("t20-completed")
    with page.expect_download() as download:
        page.locator('a[href$="relatorio.csv"]').click()
    assert download.value.suggested_filename.endswith(".csv")
    assert_policed_roles()
    assert all("status of 422" in error for error in opened.console_errors)


def test_notice_preview_shows_the_remaining_daily_quota_of_the_queue(
    browser_server: LiveServer, open_page: OpenPage, settings: SettingsWrapper
) -> None:
    settings.DELIVERY_DAILY_CAP = 100
    setup, preview = notice_preview_with_reservations("bulk-browser-quota", reserved=97)
    foreign = make_firm("bulk-browser-quota-foreign")
    graph = setup.graph
    firm = Firm(
        tenant=graph.tenant,
        owner=graph.actor,
        accountant=graph.staff,
        clients=[graph.client],
    )
    live = LiveFirm(firm=firm, port=browser_server.thread.port)
    opened = open_page(live, graph.actor)
    page = opened.page
    opened.goto(live.url("/comunicacao/"))
    expect(page.get_by_text("Reservas disponíveis hoje: 3.")).to_be_visible()
    opened.goto(live.url(f"/ciclos/lotes/{preview.pk}/"))
    quota = page.locator("[data-bulk-quota]")
    over_quota = page.locator("[data-bulk-quota-short]")
    expect(quota).to_be_visible()
    expect(quota).to_have_text("3")
    expect(over_quota).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    opened.screenshot("t20-notice-quota")

    def reload() -> None:
        response = page.reload()
        assert_policed_roles()
        assert response is not None
        assert response.ok

    # Another firm exhausting its own quota leaves this firm's value alone.
    reserve_today(foreign.tenant.pk, 100)
    reload()
    expect(quota).to_have_text("3")
    # Reloading the same preview after this firm's reservations rise is current.
    reserve_today(graph.tenant.pk, 100)
    reload()
    expect(quota).to_have_text("0")
    expect(over_quota).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    opened.screenshot("t20-notice-quota-exhausted")
    opened.goto(live.url("/comunicacao/"))
    expect(page.get_by_text("Reservas disponíveis hoje: 0.")).to_be_visible()
    opened.assert_clean_console()

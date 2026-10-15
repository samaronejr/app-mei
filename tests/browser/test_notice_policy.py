"""Real owner forms, persisted revisions and denials in both firm shells."""

from collections.abc import Callable
from http import HTTPStatus

import pytest
from playwright.sync_api import Page, expect

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.delivery.models import AutomationPause, TenantNoticePolicy
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles, axe_check

pytestmark = pytest.mark.browser
PATH = "/gestao/politica-avisos/"
FEATURES = [("import_r1",), ("cycles_r1",), ("import_r1", "cycles_r1")]


def _post(page: Page, action: str) -> int:
    with page.expect_response(
        lambda response: response.request.method == "POST" and PATH in response.url,
        timeout=10000,
    ) as response:
        page.locator(f'button[value="{action}"]').click()
    return response.value.status


@pytest.mark.parametrize("features", FEATURES)
def test_owner_saves_revokes_and_pauses_actual_policy(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    features: tuple[str, ...],
) -> None:
    live = live_firm("notice-policy-browser", features=features)
    opened = open_page(live, None)
    opened.goto(live.url(PATH))
    page = opened.page
    expect(page.locator('input[name="expected_version"]').first).to_have_value("0")
    expect(page.locator('button[value="revoke"]')).to_have_count(0)
    expect(page.locator('input[name="acknowledged"]')).not_to_be_checked()
    with tenant_context(live.firm.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()
    page.locator('input[name="kinds"][value="portal_invite"]').check()
    page.locator('input[name="acknowledged"]').check()
    assert _post(page, "save") == HTTPStatus.FOUND
    expect(page.locator('input[name="expected_version"]').first).to_have_value("1")
    expect(page.locator('input[name="acknowledged"]')).not_to_be_checked()
    with tenant_context(live.firm.tenant.pk):
        policy = TenantNoticePolicy.objects.get()
        assert policy.acknowledged_by_id == live.firm.owner.pk
        assert policy.version == policy.acknowledged_version == 1
    axe_check(page)
    opened.screenshot("AXR1-053-policy-" + "-".join(features))
    assert _post(page, "revoke") == HTTPStatus.FOUND
    expect(page.locator('input[name="expected_version"]').first).to_have_value("2")
    for action in ("pause", "resume"):
        assert _post(page, action) == HTTPStatus.FOUND
        expect(page.locator(f'button[value="{action}"]')).to_have_count(0)
    with tenant_context(live.firm.tenant.pk):
        policy.refresh_from_db()
        assert policy.grants_json == []
        assert policy.acknowledged_by_id is None
        assert AutomationPause.objects.get().paused is False
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    axe_check(page)
    opened.assert_clean_console()
    assert_policed_roles()


def test_missing_acknowledgement_stale_form_and_non_owner_are_real_denials(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm("notice-validation-browser")
    first, stale = open_page(live, None), open_page(live, None)
    first.goto(live.url(PATH))
    stale.goto(live.url(PATH))
    first.page.locator('input[name="kinds"][value="notice"]').check()
    assert _post(first.page, "save") == HTTPStatus.UNPROCESSABLE_ENTITY
    with tenant_context(live.firm.tenant.pk):
        assert not TenantNoticePolicy.objects.exists()
    first.page.locator('input[name="acknowledged"]').check()
    assert _post(first.page, "save") == HTTPStatus.FOUND
    stale.page.locator('input[name="kinds"][value="notice"]').check()
    stale.page.locator('input[name="acknowledged"]').check()
    assert _post(stale.page, "save") == HTTPStatus.CONFLICT
    with tenant_context(live.firm.tenant.pk):
        policy = TenantNoticePolicy.objects.get()
        assert policy.version == 1
    staff = open_page(live, live.firm.accountant)
    response = staff.page.goto(live.url(PATH))
    assert response is not None
    assert response.status == HTTPStatus.FORBIDDEN
    expect(staff.page.locator('input[name="kinds"]')).to_have_count(0)
    assert str(policy.pk) not in staff.page.content()
    assert str(policy.acknowledged_by_id) not in staff.page.content()
    # Expected HTTP refusal console lines are not CSP or JavaScript errors.
    for opened, status in ((first, 422), (stale, 409), (staff, 403)):
        assert all(f"status of {status}" in error for error in opened.console_errors)
    assert_policed_roles()

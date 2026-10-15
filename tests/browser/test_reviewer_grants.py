"""Real CSRF forms retain a newer revocation when an old browser tab submits."""

from collections.abc import Callable
from http import HTTPStatus

import pytest
from django.urls import reverse
from playwright.sync_api import Locator, expect

from apps.accounts.models import User
from apps.tenants.models import Membership, TenantRole
from tests.browser.harness import BrowserPage, LiveFirm
from tests.ui.factories import add_member

pytestmark = pytest.mark.browser


def _submit(shell: BrowserPage, form: Locator, url: str, status: HTTPStatus) -> None:
    with shell.page.expect_response(
        lambda response: response.url == url and response.request.method == "POST",
        timeout=10000,
    ) as submitted:
        form.get_by_role("button").click()
    assert submitted.value.status == status


def test_two_browser_callers_cannot_restore_revoked_review_authority(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    live = live_firm()
    ops = add_member(
        live.firm.tenant, "ops@browser.example.com", TenantRole.OPERATIONS_ADMIN
    )
    membership = Membership.objects.get(
        user=live.firm.accountant, tenant=live.firm.tenant
    )
    path = reverse("member-review-toggle", args=[membership.pk])
    selector = f'form[action="{path}"]'
    owner = open_page(live, None)
    stale = open_page(live, None)
    other = open_page(live, ops)
    for shell in (owner, stale):
        shell.goto(live.url(reverse("team")))
        expect(
            shell.page.locator(selector).locator('[name="expected_version"]')
        ).to_have_value("0")

    _submit(owner, owner.page.locator(selector), live.url(path), HTTPStatus.FOUND)
    expect(owner.page.locator(selector).get_by_role("button")).to_have_attribute(
        "aria-pressed", "true"
    )
    other.goto(live.url(reverse("team")))
    expect(
        other.page.locator(selector).locator('[name="expected_version"]')
    ).to_have_value("1")
    _submit(other, other.page.locator(selector), live.url(path), HTTPStatus.FOUND)
    expect(other.page.locator(selector).get_by_role("button")).to_have_attribute(
        "aria-pressed", "false"
    )

    _submit(stale, stale.page.locator(selector), live.url(path), HTTPStatus.CONFLICT)
    expect(stale.page.get_by_role("alert")).to_be_visible()
    expect(
        stale.page.locator(selector).locator('[name="expected_version"]')
    ).to_have_value("2")
    expect(stale.page.locator(selector).get_by_role("button")).to_have_attribute(
        "aria-pressed", "false"
    )
    stale.screenshot("review-grant-conflict")

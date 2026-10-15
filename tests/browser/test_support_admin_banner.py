"""The selected support grant stays visible throughout the Django admin."""

from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus

import pytest
from django.conf import settings
from playwright.sync_api import expect

from apps.accounts.models import User
from tests.authz.test_support_grant import _grant_for, _session_for, _staff
from tests.browser.harness import BrowserPage, LiveFirm, axe_check


@pytest.mark.parametrize(
    "path",
    ["/admin/", "/admin/audit/event/", "/admin/clients/clientcompany/"],
)
def test_support_banner_is_visible_on_admin_pages(
    path: str,
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
) -> None:
    # Given an owner-approved grant selected through the real admin selector.
    live = live_firm("admin-banner", features=("cycles_r1", "firm_settings_r1"))
    staff = _staff()
    staff.is_superuser = True
    staff.save(update_fields=["is_superuser"])
    grant = _grant_for(live.firm.tenant, live.firm.owner, staff)
    selected = _session_for(staff, "localhost")
    response = selected.post(
        "/admin/selecionar-empresa/",
        {"tenant": str(live.firm.tenant.pk), "reason": str(grant.pk)},
    )
    assert response.status_code == HTTPStatus.FOUND
    opened = open_page(live, staff)
    if opened.viewport == "mobile":
        opened.page.set_viewport_size({"width": 360, "height": 800})
    opened.page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": selected.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": f"http://localhost:{live.port}/",
            }
        ]
    )

    # When an admin page renders the selected grant.
    opened.goto(f"http://localhost:{live.port}{path}")

    # Then a visible status carries the actual expiry, in light and dark mode.
    banner = opened.page.locator("[data-support-banner]")
    expect(banner).to_be_visible()
    assert banner.get_attribute("role") == "status"
    assert grant.expires_at is not None
    expiry = banner.locator("time").get_attribute("datetime")
    assert expiry is not None
    assert datetime.fromisoformat(expiry) == grant.expires_at
    for theme in ("light", "dark"):
        # Set the native preference before navigation: switching an already
        # painted page animates link colors and gives axe an intermediate frame.
        opened.page.evaluate("(theme) => localStorage.setItem('theme', theme)", theme)
        opened.goto(f"http://localhost:{live.port}{path}")
        expect(opened.page.locator("html")).to_have_attribute("data-theme", theme)
        expect(banner).to_be_visible()
        axe_check(opened.page)
        opened.screenshot(f"admin-banner-{path.strip('/').replace('/', '-')}-{theme}")
    opened.assert_clean_console()

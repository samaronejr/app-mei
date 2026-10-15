"""S15 K in a real browser: the engagement ended, and the portal says so.

The client uses the portal while the engagement is current. The firm records the
engagement's end; once the offboarding grace has passed, the portal refuses the
client with a Portuguese page that names why (the engagement check), and after the
nightly sweep deactivates the seat it still refuses with the shared refusal page,
showing no company data either time, while the firm's evidence stays intact.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from django.utils.timezone import localdate
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.clients.maintenance import EngagementData, save_engagement
from apps.clients.offboarding import offboard_engagements
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceRequest
from apps.portal.middleware import ACCESS_ENDED
from apps.tenants.models import Membership
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import live_graph, open_portal, record
from tests.portal.request_fixtures import portal_setup

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


def test_journey_k_ended_engagement_closes_the_portal_with_a_message(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.ENGAGEMENT_OFFBOARDING_GRACE_DAYS = 0
    setup = portal_setup("journey-k")
    live = live_graph(setup.graph, browser_server.thread.port, "firm_settings_r1")
    portal = open_portal(open_page, live, setup.portal_user)
    home = live.url("/", portal=True)
    portal.goto(home)
    expect(portal.page.locator("h1")).to_have_text(setup.graph.client.legal_name)
    record(portal, "k-01-portal-current")

    tenant = setup.graph.tenant
    with tenant_context(tenant.pk):
        engagement = setup.graph.engagement
        engagement.refresh_from_db()
        save_engagement(
            actor=setup.graph.actor,
            tenant_id=tenant.pk,
            client_id=setup.graph.client.pk,
            engagement_id=engagement.pk,
            expected_version=engagement.version,
            data=EngagementData(
                engagement.service_kind,
                engagement.starts_on,
                ends_on=localdate() - timedelta(days=1),
                responsible_id=setup.graph.actor.pk,
            ),
        )
    refused = portal.page.goto(home)
    assert refused is not None
    assert refused.status == 403
    expect(portal.page.locator("html")).to_have_attribute("lang", "pt-br")
    expect(portal.page.locator("[data-refusal-reason]")).to_have_text(ACCESS_ENDED)
    expect(portal.page.locator("body")).not_to_contain_text(setup.request.title)
    expect(portal.page.locator("body")).not_to_contain_text(
        setup.graph.client.legal_name
    )
    record(portal, "k-02-access-ended")

    offboard_engagements(tenant.pk, on=localdate())
    with tenant_context(tenant.pk):
        assert not Membership.objects.filter(
            user=setup.portal_user, client=setup.graph.client, is_active=True
        ).exists()
        assert EvidenceRequest.objects.filter(pk=setup.request.pk).exists()
    swept = portal.page.goto(home)
    assert swept is not None
    assert swept.status == 403
    expect(portal.page.locator("html")).to_have_attribute("lang", "pt-br")
    expect(portal.page.locator("[data-refusal-reason]")).to_be_visible()
    expect(portal.page.locator("body")).not_to_contain_text(
        setup.graph.client.legal_name
    )
    record(portal, "k-03-after-sweep")
    assert_policed_roles()
    assert all("status of 403" in error for error in portal.console_errors), (
        portal.console_errors
    )

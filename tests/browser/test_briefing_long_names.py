"""A maximum-length client name wraps in its Hoje item, never widening the page."""

from collections.abc import Callable

import pytest
from django.db import models
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus
from tests.briefing.fixtures import NOW
from tests.browser.harness import BrowserPage, LiveFirm
from tests.ui.factories import add_client

pytestmark = pytest.mark.browser

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
URL = "/?escopo=team&periodo=2026-09"


def test_maximum_length_client_name_does_not_scroll_the_page_sideways(
    live_firm: Callable[..., LiveFirm],
    open_page: OpenPage,
) -> None:
    field = ClientCompany._meta.get_field("legal_name")
    assert isinstance(field, models.CharField)
    assert field.max_length is not None
    longest = "A" * field.max_length
    live = live_firm(features=("cycles_r1", "briefing_r1"))
    client = add_client(live.firm, legal_name=longest, base="112223330001")
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
    opened = open_page(live, None)
    page = opened.page
    opened.goto(live.url(URL))

    item = page.locator("[data-briefing-item]")
    expect(item).to_have_count(1)
    expect(item).to_contain_text(longest)
    widths = page.evaluate(
        "({scroll: document.documentElement.scrollWidth,"
        " client: document.documentElement.clientWidth})"
    )
    opened.screenshot("AXR1-019-long-name")
    assert widths["scroll"] <= widths["client"], widths
    opened.assert_clean_console()

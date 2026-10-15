"""axe-core on the anonymous public and help pages, at 1440x900 and 360x760.

T29 added `/ajuda/` (index + nine articles) plus apex-only `/produto/` and
`/precos/`. The existing axe sweep (test_a11y_axe) covers signed-in firm and
portal screens only, so these anonymous pages get their own sweep here: each
route is opened unsigned-in, the single visible H1 is asserted, axe runs, and
every serious or critical violation across the whole matrix is collected before
the test fails, so one run names them all.

360 px (not the 390 of the shared fixture) is the narrow width the T29 reviewer
required for these pages. An axe pass is not a WCAG claim: the manual NVDA and
VoiceOver passes in ops/AXORIM-A11Y-CHECKLIST.md stay open with the operator.
"""

from collections.abc import Iterator
from typing import Final

import pytest
from django.urls import reverse
from playwright.sync_api import Browser, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from apps.core.public_views import HELP_PAGES
from tests.browser.harness import axe_check
from tests.ui.factories import make_firm

PUBLIC_WIDTHS: Final = {
    "desktop-1440": ViewportSize(width=1440, height=900),
    "mobile-360": ViewportSize(width=360, height=760),
}

ROUTES: Final = [
    "help-index",
    *(page.url_name for page in HELP_PAGES),
    "public-product",
    "public-pricing",
]


@pytest.fixture(params=sorted(PUBLIC_WIDTHS))
def public_page(
    request: pytest.FixtureRequest,
    browser: Browser,
    browser_server: LiveServer,
) -> Iterator[tuple[Page, str]]:
    """An anonymous page on the live server at one public-surface width."""
    width = str(request.param)
    context = browser.new_context(
        viewport=PUBLIC_WIDTHS[width],
        is_mobile=width.startswith("mobile"),
        has_touch=width.startswith("mobile"),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
    )
    page = context.new_page()
    yield page, width
    context.close()


def test_axe_on_every_public_and_help_page(
    public_page: tuple[Page, str],
    browser_server: LiveServer,
) -> None:
    page, width = public_page
    apex = f"http://localhost:{browser_server.thread.port}"
    failures: dict[str, str] = {}
    for name in ROUTES:
        url = f"{apex}{reverse(name)}"
        response = page.goto(url)
        assert response is not None, url
        assert response.status == 200, (url, response.status)
        expect(page.locator("h1")).to_have_count(1)
        try:
            axe_check(page)
        except AssertionError as violation:
            failures[name] = str(violation)
    assert failures == {}, f"width {width}:\n\n" + "\n\n".join(
        f"{k}: {v}" for k, v in failures.items()
    )


def test_help_pages_on_a_firm_host_also_pass_axe(
    public_page: tuple[Page, str],
    browser_server: LiveServer,
) -> None:
    page, width = public_page
    firm = make_firm("ajuda-axe")
    base = f"http://{firm.host}:{browser_server.thread.port}"
    names = ["help-index", *(page.url_name for page in HELP_PAGES)]
    failures: dict[str, str] = {}
    for name in names:
        url = f"{base}{reverse(name)}"
        response = page.goto(url)
        assert response is not None, url
        assert response.status == 200, (url, response.status)
        try:
            axe_check(page)
        except AssertionError as violation:
            failures[name] = str(violation)
    assert failures == {}, f"firm host, width {width}:\n\n" + "\n\n".join(
        f"{k}: {v}" for k, v in failures.items()
    )

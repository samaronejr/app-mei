"""T63: axe on the R1 upload refusals and the corrected retry, at 1440x900 and 390x844.

T45 made the vault page pass. A refused upload lands somewhere else: the 400 refusal
page, which draws the layout and the upload form again so the client can retry where
she is. Under `cycles_r1` that page carries the vault's ink.

The client uses the vault's own form on the portal host of an R1 firm. A file over the
cap (moved down for the test; it is the setting the endpoint checks) is refused at
400; she follows the refusal page's link back to the vault, where an empty file is
refused at 400 as well. Axe runs on both refusal pages. The retry form on the second
one then sends a real file, which lands back on the vault with the confirmation, and
axe runs there too. Nothing is stored until that retry.

Every serious or critical violation on every step is collected before the test fails,
so one run names them all. An axe pass is not a WCAG claim.
"""

from pathlib import Path
from typing import Final

from django.urls import reverse
from playwright.sync_api import FilePayload, expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.core.tenancy import tenant_context
from apps.obligations.models import Document
from tests.browser.harness import (
    AxeViolationsError,
    BrowserPage,
    assert_policed_roles,
    axe_check,
)
from tests.browser.journeys import R1_FLAGS, OpenPage, live_graph, open_portal
from tests.portal.request_fixtures import PortalSetup, portal_setup

PORTAL_URLCONF: Final = "apps.portal.urls"
CAP: Final = 4 * 1024
CORRECTED: Final = "nota-corrigida.pdf"


def _send(page: BrowserPage, upload: str, name: str, content: bytes) -> int:
    """Choose a file in the page's upload form, submit it, return the POST's status."""
    page.page.set_input_files(
        "#arquivo",
        files=[FilePayload(name=name, mimeType="application/pdf", buffer=content)],
    )
    with (
        page.page.expect_navigation(),
        page.page.expect_response(
            lambda response: (
                response.url.endswith(upload) and response.request.method == "POST"
            )
        ) as sent,
    ):
        page.page.locator(f"main form[action='{upload}'] [type='submit']").click()
    assert_policed_roles()
    return sent.value.status


def _documents(setup: PortalSetup, **lookups: str) -> int:
    """Count the client's documents, querying inside the tenant the manager reads."""
    with tenant_context(setup.graph.tenant.pk):
        return Document.objects.filter(client=setup.graph.client, **lookups).count()


def _measure(failures: dict[str, str], page: BrowserPage, step: str) -> None:
    try:
        axe_check(page.page)
    except AxeViolationsError as violation:
        failures[step] = str(violation)
    page.screenshot(f"AXR1-063-{step}")


def test_axe_on_the_r1_upload_refusals_and_the_corrected_retry(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.PORTAL_UPLOAD_MAX_BYTES = CAP
    vault = str(reverse("portal-documents", urlconf=PORTAL_URLCONF))
    upload = str(reverse("portal-document-upload", urlconf=PORTAL_URLCONF))
    setup = portal_setup("axe-recusa")
    live = live_graph(setup.graph, browser_server.thread.port, *R1_FLAGS)
    portal = open_portal(open_page, live, setup.portal_user)
    before = _documents(setup)
    failures: dict[str, str] = {}
    refusal = portal.page.locator("main .notice-error")

    portal.goto(live.url(vault, portal=True))
    assert _send(portal, upload, "nota-grande.pdf", b"%PDF-1.4\n" + b"0" * CAP) == 400
    expect(refusal).to_be_visible()
    _measure(failures, portal, "recusa-grande")

    portal.page.locator(f"main a.link[href='{vault}']").click()
    expect(portal.page).to_have_url(live.url(vault, portal=True))
    assert _send(portal, upload, "vazio.pdf", b"") == 400
    expect(refusal).to_be_visible()
    _measure(failures, portal, "recusa-vazio")
    assert _documents(setup) == before, "a refused upload stored a document"

    assert _send(portal, upload, CORRECTED, b"%PDF-1.4 corrigida\n") == 302
    expect(portal.page).to_have_url(live.url(vault, portal=True))
    expect(portal.page.locator("#messages .notice-success")).to_be_visible()
    _measure(failures, portal, "reenvio-aceito")
    assert _documents(setup) == before + 1
    assert _documents(setup, original_filename=CORRECTED) == 1

    assert failures == {}, "\n\n".join(f"{k}: {v}" for k, v in failures.items())
    # Each refusal is a 400 document, which Chromium reports on the console; anything
    # else there -- a CSP refusal, an uncaught error -- still fails.
    assert all("status of 400" in error for error in portal.console_errors), (
        portal.console_errors
    )
    assert_policed_roles()

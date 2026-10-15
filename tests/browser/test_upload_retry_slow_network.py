"""S15 O, slow-network half: an upload sent while offline is retried on a slow link.

The connection is down (DevTools network emulation, offline) when the client sends
the file: the request fails, and the control says so in its live status line, keeps
the chosen file and offers "Tentar de novo"; nothing is recorded server-side. Back
online on an emulated slow connection, the retry's progress is visible while the
same file goes up, and the request turns "Recebido".

Chromium does not fail a request already in flight when emulation goes offline, so
the drop is placed before the send rather than in the middle of it.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import FilePayload, expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.models import EvidenceLink
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import live_graph, open_portal, record
from tests.portal.request_fixtures import portal_setup

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
KIB = 1024
PAYLOAD = b"%PDF-1.4 slow network\n" + b"0" * (512 * KIB)
SLOW = {"latency": 150, "downloadThroughput": 256 * KIB, "uploadThroughput": 48 * KIB}
# What htmx and Chromium log when a request dies with the connection: expected here.
OFFLINE_NOISE = ("htmx:sendError", "htmx:afterRequest", "ERR_INTERNET_DISCONNECTED")


def test_upload_survives_a_dropped_connection_and_retries_to_recebido(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("journey-o")
    live = live_graph(setup.graph, browser_server.thread.port)
    portal = open_portal(open_page, live, setup.portal_user)
    page = portal.page
    url = live.url(f"/solicitacoes/{setup.request.pk}/", portal=True)
    portal.goto(url)
    expect(page.locator("#request-status .badge")).to_have_text("Pendente")

    network = page.context.new_cdp_session(page)
    network.send("Network.enable")
    network.send("Network.emulateNetworkConditions", {"offline": True, **SLOW})
    page.set_input_files(
        "#arquivo",
        files=[
            FilePayload(name="extrato.pdf", mimeType="application/pdf", buffer=PAYLOAD)
        ],
    )
    record(portal, "o-01-file-chosen-offline")
    page.get_by_role("button", name="Enviar arquivo").click()
    retry = page.get_by_role("button", name="Tentar de novo")
    expect(retry).to_be_visible()
    expect(page.locator("#arquivo-estado")).to_contain_text("Não foi possível enviar")
    progress = page.locator("progress.upload-progress")
    expect(progress).to_be_hidden()
    assert page.locator("#arquivo").evaluate("input => input.files.length") == 1
    with tenant_context(live.firm.tenant.pk):
        assert not EvidenceLink.objects.filter(request=setup.request).exists()
    record(portal, "o-02-offline-retry")
    assert portal.console_errors, "the dropped request left no trace on the console"
    assert all(
        any(noise in error for noise in OFFLINE_NOISE)
        for error in portal.console_errors
    ), portal.console_errors
    portal.console_errors.clear()

    network.send("Network.emulateNetworkConditions", {"offline": False, **SLOW})
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/solicitacoes/{setup.request.pk}/enviar")
        ),
        timeout=90000,
    ) as sent:
        retry.click()
        expect(progress).to_be_visible()
        expect(progress).not_to_have_attribute("value", "0", timeout=15000)
        portal.screenshot("AXR1-025-o-03-slow-retry-in-flight")
    assert sent.value.status == 200
    expect(page.locator("#request-status .badge")).to_have_text("Recebido")
    expect(page.locator("#request-status")).to_contain_text("extrato.pdf")
    expect(page.locator("#arquivo-estado")).to_contain_text("Arquivo enviado")
    record(portal, "o-04-recebido")
    with tenant_context(live.firm.tenant.pk):
        assert EvidenceLink.objects.filter(request=setup.request).count() == 1
    portal.assert_clean_console()
    assert_policed_roles()

"""AXRM-005: the scan-required vault at 1440x900 and 390x844, with axe on every page.

A client of a firm carrying `scan_required` uses the vault's own form. With scanning
switched off her upload is refused at 400 with the antivirus sentence and nothing is
stored. With the scanner on (the fake clamd) a file of a refused type gets the
type sentence, and a clean PDF is accepted, scanned by the sweep, listed and served.
"""

from pathlib import Path
from typing import Final

from django.urls import reverse
from playwright.sync_api import FilePayload, expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.core.tenancy import tenant_context
from apps.cycles.tasks import scan_tenant_quarantine
from apps.obligations.models import Document
from tests.browser.harness import (
    AxeViolationsError,
    BrowserPage,
    assert_policed_roles,
    axe_check,
)
from tests.browser.journeys import R1_FLAGS, OpenPage, live_graph, open_portal
from tests.fakes.clamd import fake_clamd
from tests.portal.request_fixtures import PortalSetup, portal_setup

PORTAL_URLCONF: Final = "apps.portal.urls"
CLEAN: Final = "nota-verificada.pdf"
CLEAN_BYTES: Final = b"%PDF-1.4\n% nota sintetica verificada\n%%EOF\n"
SCANNER_OFF: Final = (
    "Envio temporariamente indisponível: a verificação antivírus está desligada."
)
TYPE_REFUSED: Final = "Este arquivo não foi aceito."


def _send(page: BrowserPage, upload: str, name: str, content: bytes) -> int:
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


def _documents(setup: PortalSetup) -> int:
    with tenant_context(setup.graph.tenant.pk):
        return Document.objects.filter(client=setup.graph.client).count()


def _measure(failures: dict[str, str], page: BrowserPage, step: str) -> None:
    try:
        axe_check(page.page)
    except AxeViolationsError as violation:
        failures[step] = str(violation)
    page.screenshot(f"AXRM-005-{step}")


def test_scan_required_vault_refuses_explains_and_serves_only_verified_files(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    settings.CLAMAV_ENABLED = False
    vault = str(reverse("portal-documents", urlconf=PORTAL_URLCONF))
    upload = str(reverse("portal-document-upload", urlconf=PORTAL_URLCONF))
    setup = portal_setup("axe-antivirus")
    live = live_graph(
        setup.graph, browser_server.thread.port, *R1_FLAGS, "scan_required"
    )
    portal = open_portal(open_page, live, setup.portal_user)
    failures: dict[str, str] = {}
    refusal = portal.page.locator("main .notice-error")

    portal.goto(live.url(vault, portal=True))
    assert _send(portal, upload, CLEAN, CLEAN_BYTES) == 400
    expect(refusal).to_contain_text(SCANNER_OFF)
    expect(portal.page.locator(f"main form[action='{upload}']")).to_have_count(0)
    _measure(failures, portal, "recusa-antivirus")
    assert _documents(setup) == 0, "the scanner-off refusal stored a document"

    with fake_clamd() as clamd:
        settings.CLAMAV_ENABLED = True
        settings.CLAMD_HOST = "127.0.0.1"
        settings.CLAMD_PORT = clamd.port
        portal.page.locator(f"main a.link[href='{vault}']").click()
        expect(portal.page).to_have_url(live.url(vault, portal=True))
        page_bytes = b"<!doctype html><script>alert(1)</script>"
        assert _send(portal, upload, "pagina.pdf", page_bytes) == 400
        expect(refusal).to_contain_text(TYPE_REFUSED)
        _measure(failures, portal, "recusa-tipo")

        assert _send(portal, upload, CLEAN, CLEAN_BYTES) == 302
        expect(portal.page.locator("#messages .notice-success")).to_be_visible()
        expect(portal.page.get_by_text(CLEAN)).to_have_count(0)
        assert scan_tenant_quarantine(str(setup.graph.tenant.pk)) == {"verificado": 1}
        assert len(clamd.scanned) == 1

    portal.goto(live.url(vault, portal=True))
    expect(portal.page.locator("main p", has_text=CLEAN)).to_have_count(1)
    download = portal.page.locator("main a[href^='/documentos/']").filter(
        has_not=portal.page.locator("[href='/documentos/enviar']")
    )
    expect(download).to_have_count(1)
    _measure(failures, portal, "verificado")
    served = portal.page.request.get(
        live.url(str(download.get_attribute("href")), portal=True)
    )
    assert served.status == 200
    assert served.body() == CLEAN_BYTES

    assert failures == {}, "\n\n".join(f"{k}: {v}" for k, v in failures.items())
    assert all("status of 400" in error for error in portal.console_errors), (
        portal.console_errors
    )
    assert_policed_roles()

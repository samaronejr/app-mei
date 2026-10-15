"""S15 B in a real browser: the client uploads another company's document.

The client sends an NF-e XML whose emitter CNPJ belongs to the firm's other client.
The portal first shows it received; once the scan and the extraction ran (the worker
steps), it no longer counts as sent. The firm's Hoje lists it as a divergent document,
the item leads to the review workspace, which names the divergent CNPJ, and the cycle
carries the blocking exception. Nothing is written under the other client.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from pathlib import Path

from django.utils import timezone
from playwright.sync_api import FilePayload, expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, IntakeState
from apps.cycles.extraction.service import EXCEPTION_REASON, extract_intake
from apps.cycles.scanning import FakeScanner, scan_intake
from apps.cycles.services.exceptions import reason_for
from apps.obligations.models import Document
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import R1_FLAGS, live_graph, open_portal, record
from tests.cycles.document_fixtures import fixture
from tests.portal.request_fixtures import portal_setup

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]
NAME = "nfe_outra_empresa.xml"


def test_journey_b_divergent_upload_is_held_and_raised_to_the_firm(
    browser_server: LiveServer,
    open_page: OpenPage,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("journey-b")
    live = live_graph(setup.graph, browser_server.thread.port, *R1_FLAGS)
    portal = open_portal(open_page, live, setup.portal_user)
    request_url = live.url(f"/solicitacoes/{setup.request.pk}/", portal=True)
    portal.goto(request_url)
    portal.page.set_input_files(
        "#arquivo",
        files=[
            FilePayload(name=NAME, mimeType="application/xml", buffer=fixture(NAME))
        ],
    )
    with portal.page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/solicitacoes/{setup.request.pk}/enviar")
        )
    ) as sent:
        portal.page.get_by_role("button", name="Enviar arquivo").click()
    assert sent.value.status == 200
    chip = portal.page.locator("#request-status [data-status]")
    expect(chip).to_have_attribute("data-status", "recebido")
    record(portal, "b-01-client-sent")

    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get(document__original_filename=NAME)
    scan_intake(
        setup.graph.tenant.pk, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    extract_intake(setup.graph.tenant.pk, intake.pk, now=timezone.now())
    with tenant_context(setup.graph.tenant.pk):
        intake.refresh_from_db()
        assert intake.state == IntakeState.DIVERGENT
        assert not Document.objects.filter(client=setup.other_client).exists()

    portal.goto(request_url)
    expect(chip).not_to_have_attribute("data-status", "recebido")
    record(portal, "b-02-client-not-counted")

    staff = open_page(live, setup.graph.actor)
    staff.goto(live.url(f"/?escopo=team&periodo={setup.cycle.period_start:%Y-%m}"))
    item = staff.page.locator('[data-briefing-kind="document"]')
    expect(item).to_have_count(1)
    record(staff, "b-03-hoje-divergent-item")
    item.get_by_role("link", name="Revisar vínculo e documento").click()
    expect(staff.page.locator("[data-note='cnpj_divergente']")).to_be_visible()
    record(staff, "b-04-review-divergent")

    staff.goto(live.url(f"/ciclos/{setup.cycle.pk}/"))
    reason = reason_for(EXCEPTION_REASON)
    assert reason is not None
    expect(staff.page.locator("#pendencias")).to_contain_text(reason.label)
    record(staff, "b-05-cycle-exception")
    assert_policed_roles()
    portal.assert_clean_console()
    staff.assert_clean_console()

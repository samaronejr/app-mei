"""S15 I in a real browser: SMTP down, the client uploads, the reminder stands down.

The firm asks for a reminder from the cycle page; the worker finds the SMTP peer
refusing connections and leaves it for a retry, which Comunicação shows with its
code; the client uploads through the portal meanwhile; on the retry the pre-send
re-evaluation (apps/delivery/kinds.py) cancels the reminder because the request was
answered, and an SMTP peer that is now up receives nothing. Comunicação shows the
cancellation and its reason.

Both viewports. No fixed sleep: every wait is an auto-waiting `expect` or a response.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import FilePayload, expect
from pytest_django.fixtures import SettingsWrapper
from pytest_django.live_server_helper import LiveServer

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles
from tests.browser.journeys import (
    closed_port,
    live_graph,
    open_portal,
    record,
    release_retries,
    smtp_at,
)
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup, portal_setup

OpenPage = Callable[[LiveFirm, User | None], BrowserPage]


@pytest.fixture
def journey(
    browser_server: LiveServer, settings: SettingsWrapper, tmp_path: Path
) -> tuple[PortalSetup, LiveFirm]:
    settings.MEDIA_ROOT = tmp_path
    settings.DELIVERY_PAUSED = False
    setup = portal_setup("journey-i")
    return setup, live_graph(setup.graph, browser_server.thread.port)


def _message(setup: PortalSetup, kind: str) -> OutboundMessage:
    with tenant_context(setup.graph.tenant.pk):
        return OutboundMessage.objects.get(kind=kind)


def test_journey_i_upload_during_smtp_outage_cancels_the_reminder(
    journey: tuple[PortalSetup, LiveFirm], open_page: OpenPage
) -> None:
    setup, live = journey
    staff = open_page(live, setup.graph.actor)
    staff.goto(live.url(f"/ciclos/{setup.cycle.pk}/"))
    with staff.page.expect_response(
        lambda response: (
            response.request.method == "POST" and "/solicitacoes/" in response.url
        )
    ) as reminded:
        staff.page.get_by_role("button", name="Lembrar cliente").click()
    assert reminded.value.status == 302
    reminder = _message(setup, "request_reminder")
    assert reminder.state == MessageState.QUEUED

    with smtp_at(closed_port()):
        dispatch_outbox()
    reminder = _message(setup, "request_reminder")
    assert reminder.state == MessageState.TEMPORARY
    staff.goto(live.url("/comunicacao/"))
    waiting = staff.page.locator(f'article[aria-labelledby="message-{reminder.pk}"]')
    expect(waiting.locator("[data-state]")).to_have_attribute(
        "data-state", MessageState.TEMPORARY
    )
    expect(waiting.locator("[data-state]")).to_have_text(
        f"Estado atual: {MessageState.TEMPORARY.label}"
    )
    expect(waiting).to_contain_text(reminder.last_error_code)
    record(staff, "i-01-smtp-down")

    portal = open_portal(open_page, live, setup.portal_user)
    portal.goto(live.url(f"/solicitacoes/{setup.request.pk}/", portal=True))
    portal.page.set_input_files(
        "#arquivo",
        files=[
            FilePayload(
                name="nota-setembro.pdf",
                mimeType="application/pdf",
                buffer=b"%PDF-1.4 enviado durante a falha do SMTP\n",
            )
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
    expect(portal.page.locator("#request-status .badge")).to_have_text("Recebido")
    record(portal, "i-02-client-uploaded")

    release_retries(setup.graph.tenant)
    with FakeSMTP() as server, smtp_at(server.port):
        dispatch_outbox()
    assert server.received == []
    reminder = _message(setup, "request_reminder")
    assert reminder.state == MessageState.CANCELLED
    assert reminder.last_error_code == "request_submitted"

    staff.goto(live.url("/comunicacao/"))
    cancelled = staff.page.locator(f'article[aria-labelledby="message-{reminder.pk}"]')
    expect(cancelled.locator("[data-state]")).to_have_attribute(
        "data-state", MessageState.CANCELLED
    )
    expect(cancelled.locator("[data-state]")).to_have_text(
        f"Estado atual: {MessageState.CANCELLED.label}"
    )
    expect(cancelled).to_contain_text("request_submitted")
    expect(cancelled.get_by_role("button", name="Cancelar envio")).to_have_count(0)
    record(staff, "i-03-reminder-cancelled")
    assert_policed_roles()
    staff.assert_clean_console()
    portal.assert_clean_console()

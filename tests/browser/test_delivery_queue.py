"""Real queue decisions, cancellation and accessibility at both viewports."""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from apps.delivery.tasks import dispatch_outbox
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles, axe_check
from tests.delivery.smtp import FakeSMTP
from tests.ui.factories import add_client

pytestmark = pytest.mark.browser


def test_queue_decision_and_cancel(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    live = live_firm()
    company = add_client(
        live.firm, legal_name="Cliente sintético de comunicação", base="112223330001"
    )
    settings.MEDIA_ROOT = tmp_path
    settings.DELIVERY_PAUSED = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "127.0.0.1"
    settings.EMAIL_USE_TLS = False
    settings.EMAIL_USE_SSL = False
    settings.EMAIL_TIMEOUT = 3
    intent = BusinessIntent(
        uuid4(),
        uuid4(),
        date(2026, 9, 1),
        date(2026, 9, 30),
        0,
        "notice",
        "email_notice",
    )
    with tenant_context(live.firm.tenant.pk):
        contact = ClientContact.objects.create(
            tenant=live.firm.tenant,
            client=company,
            name="Synthetic",
            email="synthetic@example.test",
        )
        unknown = enqueue(
            live.firm.owner, company.pk, contact.pk, intent=intent, expected_version=1
        )
    with FakeSMTP(mode="disconnect") as server:
        settings.EMAIL_PORT = server.port
        dispatch_outbox()
        assert len(server.received) == 1
    with tenant_context(live.firm.tenant.pk):
        queued = enqueue(
            live.firm.owner,
            company.pk,
            contact.pk,
            intent=BusinessIntent(
                intent.engagement,
                intent.playbook,
                intent.period_start,
                intent.period_end,
                0,
                "second",
                "email_notice",
            ),
            expected_version=2,
        )
    page = open_page(live, None)
    page.goto(live.url("/comunicacao/"))
    expect(page.page.locator('[data-state="resultado_incerto"]')).to_be_visible()
    axe_check(page.page)
    page.screenshot("AXR1-010-comunicacao")
    page.page.locator(f"#note-{unknown.pk}").fill(
        "Verificação manual registrada pela equipe."
    )
    with page.page.expect_response(
        lambda response: (
            response.request.method == "POST" and "/comunicacao/" in response.url
        ),
        timeout=10000,
    ) as decision:
        page.page.locator('button[value="sent"]').click()
    assert decision.value.status == 302
    expect(page.page.locator('[data-state="informada_enviada"]')).to_be_visible()
    with page.page.expect_response(
        lambda response: (
            response.request.method == "POST" and "/comunicacao/" in response.url
        ),
        timeout=10000,
    ) as cancellation:
        page.page.locator('button[value="cancel"]').click()
    assert cancellation.value.status == 302
    expect(page.page.locator('[data-state="cancelada"]')).to_be_visible()
    with tenant_context(live.firm.tenant.pk):
        assert (
            OutboundMessage.objects.get(pk=unknown.pk).state == MessageState.MANUAL_SENT
        )
        assert OutboundMessage.objects.get(pk=queued.pk).state == MessageState.CANCELLED
    axe_check(page.page)
    page.assert_clean_console()
    assert_policed_roles()

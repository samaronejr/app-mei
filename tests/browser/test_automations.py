"""Managers operate real outbox controls at desktop and mobile widths."""

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from django.test import override_settings
from playwright.sync_api import expect
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientContact
from apps.core.tenancy import tenant_context
from apps.delivery.models import DeliveryEffect, MessageState, OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from apps.delivery.tasks import dispatch_outbox
from tests.browser.harness import BrowserPage, LiveFirm, assert_policed_roles, axe_check
from tests.delivery.smtp import FakeSMTP
from tests.ui.factories import add_client

pytestmark = pytest.mark.browser
PATH = "/gestao/automacoes/"


def test_manager_pauses_retries_and_cancels_with_honest_uncertainty(
    live_firm: Callable[..., LiveFirm],
    open_page: Callable[[LiveFirm, User | None], BrowserPage],
    settings: SettingsWrapper,
    tmp_path: Path,
) -> None:
    # Given two real refusals and one ambiguous SMTP result.
    live = live_firm("automation-browser")
    company = add_client(
        live.firm,
        legal_name="Ateliê Horizonte - cliente sintético de operações e documentos",
        base="112223330001",
    )
    settings.MEDIA_ROOT = tmp_path
    settings.DELIVERY_PAUSED = False
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
            name="Contato sintético",
            email="automation@example.test",
        )

    def queue(occurrence: int) -> OutboundMessage:
        with tenant_context(live.firm.tenant.pk):
            company.refresh_from_db()
            return enqueue(
                live.firm.owner,
                company.pk,
                contact.pk,
                intent=replace(intent, occurrence=occurrence),
                expected_version=company.version,
            )

    retry, cancel = queue(1), queue(2)
    with (
        FakeSMTP(mode="reject") as peer,
        override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=peer.port,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            EMAIL_TIMEOUT=3,
        ),
    ):
        assert dispatch_outbox() == 2
        unknown = queue(3)
        peer.mode = "disconnect"
        assert dispatch_outbox() == 1
        page = open_page(live, None)
        page.goto(live.url("/comunicacao/"))
        page.screenshot("AXR1-027-entrypoint")
        page.page.locator(f'a[href="{PATH}"]').click()
        expect(page.page.locator("[data-job]")).not_to_have_count(0)
        unknown_row = page.page.locator(f'[data-message-id="{unknown.pk}"]')
        expect(unknown_row.locator("button")).to_have_count(0)
        axe_check(page.page)
        page.screenshot("AXR1-027-automacoes-populated")

        # When the manager submits each rendered form through the browser.
        for action, paused in (("pause", "true"), ("unpause", "false")):
            with page.page.expect_response(
                lambda response: (
                    response.request.method == "POST" and PATH in response.url
                ),
                timeout=10000,
            ) as response:
                page.page.locator(f'button[value="{action}"]').click()
            assert response.value.status == 302
            expect(
                page.page.locator(f'[data-tenant-paused="{paused}"]')
            ).to_be_visible()
        for message, action in ((retry, "retry"), (cancel, "cancel")):
            row = page.page.locator(f'[data-message-id="{message.pk}"]')
            with page.page.expect_response(
                lambda response: (
                    response.request.method == "POST" and PATH in response.url
                ),
                timeout=10000,
            ) as response:
                row.locator(f'button[value="{action}"]').click()
            assert response.value.status == 302
            expect(row).to_have_count(0)

        # Then the uncertain effect stays blocked and exactly one successor can send.
        with tenant_context(live.firm.tenant.pk):
            successor = OutboundMessage.objects.get(retry_of=retry)
            assert not OutboundMessage.objects.filter(retry_of=cancel).exists()
            assert (
                DeliveryEffect.objects.filter(
                    message_id__in=[retry.pk, cancel.pk], outcome=MessageState.REJECTED
                ).count()
                == 2
            )
            assert (
                OutboundMessage.objects.get(pk=unknown.pk).state == MessageState.UNKNOWN
            )
        peer.mode = "accept"
        assert (dispatch_outbox(), dispatch_outbox()) == (1, 0)
        assert len(peer.received) == 4
        with tenant_context(live.firm.tenant.pk):
            successor.refresh_from_db()
            assert successor.state == MessageState.ACCEPTED
        page.screenshot("AXR1-027-automacoes-resolved")
        axe_check(page.page)
        page.assert_clean_console()
        assert_policed_roles()

"""S15 I: reminder queued, SMTP down, client uploads meanwhile -> the retry cancels.

T10 shipped the registry contract (a hook re-read at send). T11 owns the real
scenario: a real request, a real reminder queued through `remind_request`, an SMTP
peer that is DOWN (connection refused) for the first dispatch, a real portal upload in
between, and the retry cancelling the reminder with the reason code -- while a peer
that is now UP receives nothing.
"""

import socket
from pathlib import Path

import pytest
from django.test import override_settings
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.services.requests import remind_request
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.tasks import dispatch_outbox
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

pytestmark = pytest.mark.django_db(transaction=True)


def _closed_port() -> int:
    """A local port nothing listens on: connecting is refused (SMTP down)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _smtp(port: int) -> override_settings:
    return override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="127.0.0.1",
        EMAIL_PORT=port,
        EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="",
        EMAIL_USE_TLS=False,
        EMAIL_USE_SSL=False,
        EMAIL_TIMEOUT=3,
    )


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    settings.DELIVERY_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    return portal_setup("reminder-s15i")


def _messages(setup: PortalSetup, kind: str) -> list[OutboundMessage]:
    with tenant_context(setup.graph.tenant.pk):
        return list(OutboundMessage.objects.filter(kind=kind).order_by("pk"))


def _release_retries(setup: PortalSetup) -> None:
    """Advance the retry clock to now: the backoff is not the behaviour under test."""
    with tenant_context(setup.graph.tenant.pk):
        OutboundMessage.objects.filter(state=MessageState.TEMPORARY).update(
            next_attempt_at=timezone.now()
        )


def test_upload_between_attempts_cancels_the_reminder_with_a_reason(
    setup: PortalSetup,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        remind_request(
            setup.graph.actor,
            setup.request.pk,
            expected_version=setup.request.version,
        )
    [reminder] = _messages(setup, "request_reminder")
    assert reminder.state == MessageState.QUEUED
    assert reminder.evidence_request_id == setup.request.pk

    with _smtp(_closed_port()):
        dispatch_outbox()
    [reminder] = _messages(setup, "request_reminder")
    assert reminder.state == MessageState.TEMPORARY
    assert reminder.last_error_code == "smtp_unavailable"

    uploaded = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("nota.pdf", b"%PDF-1.4 enviado pelo cliente\n")},
    )
    assert uploaded.status_code == 302

    _release_retries(setup)
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [reminder] = _messages(setup, "request_reminder")
    assert reminder.state == MessageState.CANCELLED
    assert reminder.last_error_code == "request_submitted"
    # The notice about the same (now answered) request is cancelled too.
    [notice] = _messages(setup, "request_notice")
    assert notice.state == MessageState.CANCELLED
    assert notice.last_error_code == "requests_answered"
    assert server.received == []
    with tenant_context(setup.graph.tenant.pk):
        assert DeliveryAttempt.objects.filter(
            message_id=reminder.pk,
            outcome=MessageState.CANCELLED,
            error_code="request_submitted",
        ).exists()


def test_without_an_upload_the_retry_sends_the_reminder(setup: PortalSetup) -> None:
    """Control: the cancellation above is caused by the upload, not by the retry."""
    with tenant_context(setup.graph.tenant.pk):
        remind_request(
            setup.graph.actor,
            setup.request.pk,
            expected_version=setup.request.version,
        )
    with _smtp(_closed_port()):
        dispatch_outbox()
    _release_retries(setup)
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [reminder] = _messages(setup, "request_reminder")
    assert reminder.state == MessageState.ACCEPTED
    assert len(server.received) == 2  # the notice and the reminder
    body = b"".join(server.received)
    assert b"Lembrete" in body


def test_a_waived_request_cancels_its_queued_reminder(setup: PortalSetup) -> None:
    from apps.cycles.services.requests import waive_request  # noqa: PLC0415

    with tenant_context(setup.graph.tenant.pk):
        remind_request(
            setup.graph.actor,
            setup.request.pk,
            expected_version=setup.request.version,
        )
        current = setup.reload(setup.request)
        waive_request(
            setup.graph.actor,
            current.pk,
            reason="cliente sem movimento",
            expected_version=current.version,
        )
    with FakeSMTP() as server, _smtp(server.port):
        dispatch_outbox()
    [reminder] = _messages(setup, "request_reminder")
    assert reminder.state == MessageState.CANCELLED
    assert reminder.last_error_code == "request_closed"
    assert server.received == []

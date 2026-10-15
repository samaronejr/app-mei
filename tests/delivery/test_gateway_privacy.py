"""Exercise production scrubbers while the actual gateway owns sensitive bytes."""

import io
import json
import logging

import pytest
import sentry_sdk
from django.core.mail import EmailMessage

from apps.delivery.tasks import dispatch_outbox
from config.settings import prod
from tests.delivery.conftest import DeliverySetup
from tests.security.test_credential_log_hygiene import _CaptureTransport

pytestmark = pytest.mark.django_db(transaction=True)


def test_gateway_scopes_real_console_and_sentry_redaction(
    delivery: DeliverySetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = delivery.queue()
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        **{
            **prod.SENTRY_OPTIONS,
            "dsn": "http://public@example.invalid/1",
            "transport": transport,
        }
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(prod.CredentialURLLogFilter())
    secrets: list[str] = []

    def instrumented_send(email: EmailMessage, **kwargs: object) -> int:
        body, recipient = str(email.body), str(email.to[0])
        secrets.extend([body, recipient])
        sentry_sdk.capture_exception(RuntimeError(f"{body} {recipient}"))
        handler.handle(
            logging.makeLogRecord({"msg": "%s %s", "args": (body, recipient)})
        )
        return 1

    monkeypatch.setattr(EmailMessage, "send", instrumented_send)
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            dispatch_outbox()
            client.flush()
    finally:
        client.close()
        handler.close()
    assert transport.events
    serialized = json.dumps(transport.events) + stream.getvalue()
    assert all(secret not in serialized for secret in secrets)
    assert message.recipients_json[0] not in serialized
    assert prod.SENTRY_REDACTION_MARKER in serialized

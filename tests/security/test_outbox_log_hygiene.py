"""Real Sentry serialization and console rendering cannot retain delivery text."""

import io
import json
import logging

import sentry_sdk

from apps.delivery.privacy import delivery_secrets
from config.settings import prod
from tests.security.test_credential_log_hygiene import _CaptureTransport

BODY = "PRIVATE_BODY_CANARY_812d"
RECIPIENT = "private-recipient-812d@example.test"


def test_real_sentry_event_scrubs_delivery_aliases() -> None:
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        **{
            **prod.SENTRY_OPTIONS,
            "dsn": "http://public@example.invalid/1",
            "transport": transport,
        }
    )
    try:
        with delivery_secrets(BODY, RECIPIENT), sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            scope.set_extra("body", BODY)
            scope.set_extra("recipients_json", [RECIPIENT])
            sentry_sdk.capture_exception(RuntimeError(f"{BODY} {RECIPIENT}"))
            client.flush()
    finally:
        client.close()
    serialized = json.dumps(transport.events)
    assert transport.events
    assert BODY not in serialized
    assert RECIPIENT not in serialized
    assert prod.SENTRY_REDACTION_MARKER in serialized


def test_console_redacts_delivery_exception_aliases() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(prod.CredentialURLLogFilter())
    record = logging.makeLogRecord(
        {
            "msg": "SMTP diagnostic %s",
            "args": (RECIPIENT,),
            "exc_info": (RuntimeError, RuntimeError(BODY), None),
        }
    )
    with delivery_secrets(BODY, RECIPIENT):
        handler.handle(record)
    assert BODY not in stream.getvalue()
    assert RECIPIENT not in stream.getvalue()
    assert prod.SENTRY_REDACTION_MARKER in stream.getvalue()


def test_structured_delivery_fields_are_scrubbed_without_scope() -> None:
    event = prod.scrub_sentry_event(
        {"extra": {"body": BODY, "recipient": RECIPIENT}}, {}
    )
    assert BODY not in json.dumps(event)
    assert RECIPIENT not in json.dumps(event)

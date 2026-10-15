"""Real Sentry serialization and console rendering cannot retain delivery text."""

import base64
import io
import json
import logging

import pytest
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


@pytest.mark.parametrize("field", ["mime_base64", "frozen_mime", "body_ptbr"])
def test_real_sentry_event_scrubs_frozen_outbox_payload_without_egress_scope(
    field: str,
) -> None:
    # Given the actual frozen-payload shapes, before a gateway redaction scope exists.
    payload = BODY if field == "body_ptbr" else base64.b64encode(BODY.encode()).decode()
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        **{
            **prod.SENTRY_OPTIONS,
            "dsn": "http://public@example.invalid/1",
            "transport": transport,
        }
    )
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            scope.set_extra("outbox_payload", {field: payload})
            scope.set_extra("delivery_event_code", "synthetic-envelope-diagnostic")

            # When the real SDK serializes the diagnostic.
            sentry_sdk.capture_message("synthetic-outbox-probe")
            client.flush()
    finally:
        client.close()

    # Then the capture is nonempty and diagnostic, but contains no frozen content.
    serialized = json.dumps(transport.events)
    assert transport.events
    assert "synthetic-envelope-diagnostic" in serialized
    assert payload not in serialized
    assert BODY not in serialized
    assert prod.SENTRY_REDACTION_MARKER in serialized

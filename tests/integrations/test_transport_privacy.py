"""Provider credentials must not escape through the existing telemetry sinks."""

import json
import logging
from io import StringIO

import pytest
import sentry_sdk

from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import ProviderRequest
from config.settings import prod
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply
from tests.security.test_credential_log_hygiene import _CaptureTransport


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "jwt_token",
        "autenticar_procurador_token",
        "X-Access-Token",
        "X-Broker-Token-HMAC",
    ],
)
def test_provider_header_is_redacted_from_sentry(key: str) -> None:
    # Given a provider credential embedded in a captured event.
    canary = "synthetic-provider-secret-781"
    # When the production Sentry hook processes it.
    event = prod.scrub_sentry_event({"extra": {key: canary}}, {})
    # Then the credential is absent, but the event remains useful.
    assert canary not in json.dumps(event)
    assert prod.SENTRY_REDACTION_MARKER in json.dumps(event)


def test_provider_query_is_redacted_from_console() -> None:
    # Given a library diagnostic containing a credential-bearing URL.
    record = logging.makeLogRecord(
        {"msg": "GET https://provider.invalid/file?token=synthetic-query-781"}
    )
    # When the installed console filter runs.
    prod.CredentialURLLogFilter().filter(record)
    # Then no provider token reaches the serialized record.
    assert "synthetic-query-781" not in record.getMessage()


@pytest.mark.parametrize(
    "template",
    [
        "Authorization: Bearer %s",
        "jwt_token=%s",
        "autenticar_procurador_token: %s",
        "X-Anything-Token-Final: %s",
        "GET /file?token=%s",
        "GET https://focusnfe.s3.sa-east-1.amazonaws.com/file?X-Amz-Signature=%s",
    ],
)
def test_formatted_console_messages_scrub_provider_credentials(template: str) -> None:
    canary = "synthetic-formatted-canary"
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(prod.CredentialURLLogFilter())
    logger = logging.getLogger("transport-canary")
    logger.addHandler(handler)
    try:
        logger.warning(template, canary)
    finally:
        logger.removeHandler(handler)
    assert canary not in output.getvalue()
    assert prod.SENTRY_REDACTION_MARKER in output.getvalue()


def test_base64_body_and_exception_chain_are_not_logged() -> None:
    payload = "YWJj" * 400
    record = logging.makeLogRecord({"msg": "body: %s", "args": (payload,)})
    prod.CredentialURLLogFilter().filter(record)
    assert payload not in record.getMessage()


def test_sentry_captures_a_real_event_without_provider_secrets() -> None:
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1",
        transport=transport,
        before_send=prod.scrub_sentry_event,
        include_local_variables=False,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        scope.set_extra("jwt_token", "synthetic-sentry-jwt")
        scope.set_extra("unrelated", "retained")
        sentry_sdk.capture_message("provider diagnostic")
        client.flush()
    client.close()
    assert len(transport.events) == 1
    encoded = json.dumps(transport.events)
    assert "synthetic-sentry-jwt" not in encoded
    assert "retained" in encoded


def test_response_body_and_presigned_url_never_reach_wire_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeFocus((Reply(body=b"synthetic-untrusted-body"),)) as peer:
        ProviderTransport("focus", "fake", peer.origin).send(
            "FC-NFE", ProviderRequest(headers=(("Authorization", "synthetic-auth"),))
        )
    assert "synthetic-untrusted-body" not in caplog.text
    assert "synthetic-auth" not in caplog.text

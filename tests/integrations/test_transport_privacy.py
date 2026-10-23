"""Provider credentials must not escape through the existing telemetry sinks."""

import json
import logging
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
import sentry_sdk

from apps.delivery.privacy import delivery_secrets
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import ProviderRequest
from config.settings import prod
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply, permitted_send
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


def test_base64_body_is_not_logged() -> None:
    payload = "YWJj" * 400
    record = logging.makeLogRecord({"msg": "body: %s", "args": (payload,)})
    prod.CredentialURLLogFilter().filter(record)
    assert payload not in record.getMessage()


CANARY = "synthetic-structured-canary-4417"


def production_console(emit: Callable[[logging.Logger], None]) -> str:
    """Real production handler: settings formatter plus the installed filter."""
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(
        logging.Formatter(prod.LOGGING["formatters"]["simple"]["format"])
    )
    for name in prod.LOGGING["handlers"]["console"]["filters"]:
        handler.addFilter(prod.LOGGING["filters"][name]["()"]())
    logger = logging.getLogger("transport-privacy-console")
    logger.addHandler(handler)
    try:
        emit(logger)
    finally:
        logger.removeHandler(handler)
    return output.getvalue()


def captured_events(emit: Callable[[], None]) -> list[Any]:
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1",
        transport=transport,
        before_send=prod.scrub_sentry_event,
        include_local_variables=False,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        emit()
        client.flush()
    client.close()
    return transport.events


@pytest.mark.parametrize(
    "argument",
    [
        [("Authorization", CANARY)],
        [["X-Access-Token", CANARY]],
        (("jwt_token", CANARY),),
        {"headers": [("Authorization", f"Bearer {CANARY}")]},
        {"body": CANARY},
        {"payload": {"nested": CANARY}},
        {"data": [CANARY]},
        f"Bearer {CANARY}",
        f"{{'body': '{CANARY}'}}",
    ],
    ids=[
        "header-pair-list",
        "header-pair-nested-list",
        "header-pair-tuple",
        "headers-mapping-of-pairs",
        "body-field",
        "payload-field-nested",
        "data-field-list",
        "bare-bearer",
        "rendered-body-field",
    ],
)
def test_structured_credentials_are_absent_from_console_output(
    argument: object,
) -> None:
    # Given a diagnostic whose argument carries a credential in structure.
    # When the production console handler formats it.
    output = production_console(
        lambda logger: logger.warning("provider diagnostic %s", argument)
    )
    # Then the credential is absent and the diagnostic itself survives.
    assert CANARY not in output
    assert "provider diagnostic" in output


def upstream_rejection() -> None:
    msg = f"upstream rejected {CANARY}"
    raise ValueError(msg)


def provider_failure(*, explicit: bool) -> None:
    """Raise a wrapper whose cause (or implicit context) mentions the canary."""
    try:
        upstream_rejection()
    except ValueError as error:
        wrapped = "provider call failed"
        if explicit:
            raise RuntimeError(wrapped) from error
        raise RuntimeError(wrapped)  # noqa: B904


@pytest.mark.parametrize("explicit", [True, False], ids=["cause", "context"])
def test_active_secret_is_absent_from_a_rendered_exception_chain(
    explicit: bool,
) -> None:
    # Given an active credential that only an inner exception mentions.
    def emit(logger: logging.Logger) -> None:
        with delivery_secrets(CANARY):
            try:
                provider_failure(explicit=explicit)
            except RuntimeError:
                logger.exception("provider failure")

    # When the production console handler renders the whole chain.
    output = production_console(emit)
    # Then both exceptions are rendered and the credential is not.
    assert "ValueError" in output
    assert "RuntimeError" in output
    assert CANARY not in output


@pytest.mark.parametrize(
    "extra",
    [
        [["Authorization", CANARY]],
        [("X-Access-Token", f"Bearer {CANARY}")],
        f"[('Authorization', '{CANARY}')]",
        f'[["autenticar_procurador_token", "{CANARY}"]]',
        f"Bearer {CANARY}",
    ],
    ids=["pair", "bearer-pair", "rendered-pair", "json-pair", "bare-bearer"],
)
def test_captured_sentry_event_drops_header_pair_credentials(extra: object) -> None:
    # Given header pairs attached to a real captured Sentry event.
    def emit() -> None:
        sentry_sdk.set_extra("headers", extra)
        sentry_sdk.set_extra("unrelated", "retained")
        sentry_sdk.capture_message("provider diagnostic")

    # When the production before_send hook processes it.
    events = captured_events(emit)
    # Then the credential is gone and unrelated context remains.
    assert len(events) == 1
    encoded = json.dumps(events)
    assert CANARY not in encoded
    assert "retained" in encoded


def test_structured_log_message_is_redacted() -> None:
    # Given a mapping logged as the message itself.
    # When the production console handler formats it.
    output = production_console(
        lambda logger: logger.warning(
            {"event": "provider diagnostic", "payload": {"nested": CANARY}}
        )
    )
    # Then the nested provider field is gone and the event survives.
    assert CANARY not in output
    assert "provider diagnostic" in output


def test_structured_fields_in_a_cause_are_absent_from_console_output() -> None:
    # Given a cause whose arguments carry a nested provider field.
    def emit(logger: logging.Logger) -> None:
        try:
            try:
                raise_structured_cause()
            except ValueError as error:
                wrapped = "provider call failed"
                raise RuntimeError(wrapped) from error
        except RuntimeError:
            logger.exception("provider failure")

    # When the production console handler renders the chain.
    output = production_console(emit)
    # Then the cause is rendered without the nested value.
    assert "ValueError" in output
    assert CANARY not in output


def raise_structured_cause() -> None:
    raise ValueError({"payload": {"nested": CANARY}})


def test_header_value_is_removed_from_every_other_event_field() -> None:
    # Given a header pair whose value is repeated in another field.
    def emit() -> None:
        sentry_sdk.set_extra("headers", [["Authorization", CANARY]])
        sentry_sdk.set_extra("note", f"retrying with {CANARY}")
        sentry_sdk.capture_message("provider diagnostic")

    # When the production hook processes the real event.
    encoded = json.dumps(captured_events(emit))
    # Then neither occurrence survives.
    assert CANARY not in encoded
    assert "retrying with" in encoded


def test_captured_sentry_exception_chain_drops_active_secret() -> None:
    # Given an exception chain whose cause mentions an active credential.
    def emit() -> None:
        with delivery_secrets(CANARY):
            try:
                provider_failure(explicit=True)
            except RuntimeError as error:
                sentry_sdk.capture_exception(error)

    # When the real client captures it.
    events = captured_events(emit)
    # Then both exceptions are present and the credential is not.
    encoded = json.dumps(events)
    assert "ValueError" in encoded
    assert "RuntimeError" in encoded
    assert CANARY not in encoded


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
        permitted_send(
            ProviderTransport("focus", "fake", peer.origin),
            "FC-NFE",
            ProviderRequest(headers=(("Authorization", "synthetic-auth"),)),
        )
    assert "synthetic-untrusted-body" not in caplog.text
    assert "synthetic-auth" not in caplog.text

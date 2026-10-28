"""Rejected records must not be inspected by the SDK's outer dispatch wrapper."""

from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING, cast

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.logging import LoggingIntegration

from apps.delivery.privacy import delivery_secrets
from config.settings import prod
from tests.security.test_redaction_boundary_regressions import (
    CANARY,
    LOGGER,
    _EveryEnvelope,
    _sinks,
)
from tests.security.test_withhold_boundaries import _MissingStack

if TYPE_CHECKING:
    from sentry_sdk._types import Log


class _NameTrap(logging.LogRecord):
    name_reads: int = 0

    def __getattribute__(self, name: str) -> object:
        if name == "name":
            reads: int = object.__getattribute__(self, "name_reads")
            object.__setattr__(self, "name_reads", reads + 1)
            message = CANARY
            raise RuntimeError(message)
        value: object = object.__getattribute__(self, name)
        return value


def test_a_rejected_record_never_reaches_sdk_name_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a valid LogRecord subclass whose name cannot safely be read.
    record = _NameTrap(LOGGER, logging.ERROR, __file__, 0, CANARY, (), None)
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        # When the console rejects it before the SDK's outer finally block.
        logger.handle(record)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then no SDK path even reads the rejected original, raises or delivers it.
    assert record.name_reads == 0
    assert stream.getvalue() == ""
    assert CANARY not in b"".join(transport.payloads).decode()
    assert len(transport.events) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", 90317), ("levelno", "not-a-level"), ("created", float("nan"))],
)
def test_malformed_standard_fields_are_withheld_before_external_formatting(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    # Given a standard carrier with an invalid dispatch or formatting field.
    record = logging.makeLogRecord(
        {"name": LOGGER, "levelno": logging.ERROR, "msg": CANARY, field: value}
    )
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        # When it passes through the real console and full SDK configuration.
        logger.handle(record)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then formatting cannot expose the original through handleError or the SDK.
    assert "[Filtered]" in stream.getvalue()
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()


def test_default_factory_headers_still_preserve_an_ordinary_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the stdlib factory's legitimate missing-name and missing-level defaults.
    message = "ordinary default record"
    record = logging.makeLogRecord({"msg": message})
    with _sinks(monkeypatch) as (logger, stream, client, transport):
        # When it is submitted through Logger.handle, not just a handler directly.
        logger.handle(record)
        client.flush()
    # Then normalization preserves the diagnostic at both real sinks.
    assert message in stream.getvalue()
    assert message in json.dumps(transport.events)


class _OpaqueSecret:
    def __str__(self) -> str:
        return CANARY

    def __repr__(self) -> str:
        return CANARY


@pytest.mark.parametrize("field", ["exc_text", "stack_info", "levelname"])
def test_rendered_standard_text_is_scrubbed_after_opaque_values_are_rendered(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # Given a known credential revealed only while a standard text field renders.
    record = logging.LogRecord(
        LOGGER, logging.ERROR, __file__, 0, "ordinary diagnostic", (), None
    )
    record.__dict__[field] = _OpaqueSecret()
    with _sinks(monkeypatch) as (logger, stream, client, transport):
        with delivery_secrets(CANARY):
            # When the real console handles it directly, with no earlier SDK pass.
            logger.handlers[-1].handle(record)
            logger.handle(record)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then neither that first console pass nor later SDK serialization exposes it.
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert "ordinary diagnostic" in stream.getvalue()


@pytest.mark.parametrize("failure", ["lookup", "handler"])
def test_an_sdk_dispatch_failure_cannot_escape_or_deliver_the_original(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Given failure before the SDK's own protected handlers have started.
    def unavailable(*_args: object) -> None:
        message = CANARY
        raise RuntimeError(message)

    with _sinks(monkeypatch) as (logger, stream, client, transport):
        with monkeypatch.context() as broken:
            if failure == "lookup":
                broken.setattr(sentry_sdk, "get_client", unavailable)
            else:
                broken.setattr(LoggingIntegration, "_handle_record", unavailable)
            # When the logging integration tries to select its client.
            with delivery_secrets(CANARY):
                logger.error(CANARY)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then no original was emitted before failure, and later ordinary work flows.
    if failure == "lookup":
        assert stream.getvalue() == ""
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert len(transport.events) == 1


def test_sdk_logs_cannot_serialize_a_known_credential_from_an_opaque_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a known value exposed only by the SDK's late attribute representation.
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        with delivery_secrets(CANARY):
            # When the logging integration emits both an event and SDK Logs.
            logger.error("ordinary diagnostic", extra={"opaque": _OpaqueSecret()})
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then every actual envelope is safe, including the log-specific payload.
    assert "log" in transport.item_types
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    logs = [
        log
        for payload in transport.payloads
        for item in Envelope.deserialize(payload)
        if item.type == "log"
        for log in json.loads(item.payload.get_bytes())["items"]
    ]
    assert [log["body"] for log in logs] == ["ordinary diagnostic"]


def test_direct_sdk_logs_freeze_opaque_attributes_before_the_private_context_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an SDK log entering the client without the logging adapter's rendering.
    log = {
        "body": "ordinary direct sdk diagnostic",
        "attributes": {"opaque": _OpaqueSecret()},
        "severity_text": "error",
        "severity_number": 17,
        "time_unix_nano": 0,
        "trace_id": None,
        "span_id": None,
    }
    with _sinks(monkeypatch, logs=True) as (_, stream, client, transport):
        with delivery_secrets(CANARY):
            # When the real client runs the production hook before enqueueing.
            client._capture_log(
                cast("Log", cast("object", log)), sentry_sdk.get_current_scope()
            )
        # Then late serialization must be safe after the credential scope is gone.
        client.flush()
    assert transport.item_types == ["log"]
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    logs = [
        item
        for payload in transport.payloads
        for envelope_item in Envelope.deserialize(payload)
        if envelope_item.type == "log"
        for item in json.loads(envelope_item.payload.get_bytes())["items"]
    ]
    assert [item["body"] for item in logs] == ["ordinary direct sdk diagnostic"]
    assert logs[0]["attributes"]["opaque"]["value"] == "[Filtered]"


class _InjectingRecordUpdate(dict[str, object]):
    def update(self, *_args: object, **_kwargs: object) -> None:
        self["name"] = CANARY


def test_a_successful_record_scrub_never_calls_its_original_mapping_mutator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a carrier mapping that injects a private logger name when updated.
    record = logging.LogRecord(
        LOGGER, logging.ERROR, __file__, 0, "ordinary diagnostic", (), None
    )
    record.__dict__ = _InjectingRecordUpdate(record.__dict__)
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        with delivery_secrets(CANARY):
            # When the console handles the first scrub without an earlier SDK pass.
            logger.handlers[-1].handle(record)
            logger.handle(record)
        client.flush()
    # Then only a new safe snapshot is committed, never the hostile mutator.
    assert "ordinary diagnostic" in stream.getvalue()
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()


def test_sdk_log_attributes_are_discovered_without_hiding_the_diagnostic_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a private attribute added by the SDK scope after record filtering.
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        sentry_sdk.get_current_scope().set_attribute("token", CANARY)
        # When the real logging integration creates an SDK log with an ordinary body.
        logger.info("ordinary sdk attribute diagnostic")
        client.flush()
    # Then attribute discovery still applies, without treating the body as a payload.
    assert transport.item_types == ["log"]
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert b"ordinary sdk attribute diagnostic" in b"".join(transport.payloads)


def test_the_log_callback_contains_its_own_context_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a valid SDK log and an unavailable private context source.
    log = {
        "body": CANARY,
        "attributes": {},
        "severity_text": "error",
        "severity_number": 17,
        "time_unix_nano": 0,
        "trace_id": None,
        "span_id": None,
    }
    monkeypatch.setattr(prod, "_task_stack", _MissingStack())
    # When the installed callback is invoked directly.
    result = prod.SENTRY_OPTIONS["before_send_log"](log, {})
    # Then it returns the SDK's drop value rather than raising its private error.
    assert result is None


def test_direct_sdk_handlers_also_reject_an_unsupported_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an SDK call that bypasses Logger.callHandlers and the console entirely.
    record = _NameTrap(LOGGER, logging.ERROR, __file__, 0, CANARY, (), None)
    with _sinks(monkeypatch, logs=True) as (_, _, client, transport):
        integration = client.get_integration(LoggingIntegration)
        assert integration is not None
        # When the installed integration's event, breadcrumb and log paths run.
        integration._handle_record(record)
        integration._handle_sentry_logs_record(record)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then each sink rejects the original before even reading its name.
    assert record.name_reads == 0
    assert transport.item_types == ["event"]
    assert CANARY not in b"".join(transport.payloads).decode()


def test_a_log_with_unavailable_credentials_never_enters_the_sdk_batcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an SDK log hook whose context source cannot be read.
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        with monkeypatch.context() as broken:
            broken.setattr(prod, "_task_stack", _MissingStack())
            # When a safe withheld record would otherwise enter the log batcher.
            logger.error(CANARY)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then even a secondary hook failure drops it, and later work still flows.
    assert "log" not in transport.item_types
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert len(transport.events) == 1


def test_the_global_dispatch_wrapper_does_not_change_a_nonproduction_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the production integration has been installed in this interpreter.
    with _sinks(monkeypatch):
        pass
    transport = _EveryEnvelope()
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1",
        transport=transport,
        before_send=None,
        before_breadcrumb=None,
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("axrm011.fix4.nonproduction")
    monkeypatch.setattr(logger, "level", logging.ERROR)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    try:
        with sentry_sdk.isolation_scope() as scope, sentry_sdk.new_scope():
            scope.clear()
            scope.set_client(client)
            with delivery_secrets(CANARY):
                # When an explicitly unhardened client logs an ordinary value.
                logger.error(CANARY)
            client.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
        client.close()
    # Then process-wide installation has not leaked production's filtering policy.
    assert CANARY in stream.getvalue()
    assert CANARY in json.dumps(transport.events)


class _FailingNonproductionHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        message = "ordinary handler failure"
        raise RuntimeError(message)


def test_a_nonproduction_clients_handler_errors_keep_the_stdlib_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the process-wide production wrapper and a different, plain client.
    with _sinks(monkeypatch):
        pass
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1",
        transport=_EveryEnvelope(),
        default_integrations=False,
    )
    handler = _FailingNonproductionHandler()
    logger = logging.getLogger("axrm011.fix4.nonproduction.failure")
    monkeypatch.setattr(logger, "level", logging.ERROR)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    try:
        with sentry_sdk.isolation_scope() as scope, sentry_sdk.new_scope():
            scope.clear()
            scope.set_client(client)
            # When a plain client's own handler raises.
            with pytest.raises(RuntimeError, match="ordinary handler failure"):
                logger.error("ordinary diagnostic")
    finally:
        logger.removeHandler(handler)
        handler.close()
        client.close()

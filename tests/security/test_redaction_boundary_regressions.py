"""Rejected diagnostics cannot re-enter through another console or SDK path."""

from __future__ import annotations

import json
import logging
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import sentry_sdk
from celery import Celery

from apps.delivery.privacy import delivery_secrets
from config.settings import prod
from tests.security.test_credential_log_hygiene import _CaptureTransport
from tests.security.test_task_failure_log_privacy import (
    _failure_logger,
    _production_console,
)
from tests.security.test_withhold_boundaries import (
    _MissingStack,
    _RejectGet,
    _RejectUpdate,
)

if TYPE_CHECKING:
    import io
    from collections.abc import Iterator

    from celery.app.task import Task
    from sentry_sdk.envelope import Envelope

CANARY = "synthetic-fix4-boundary-credential-90317"
INNER = "synthetic-fix4-inner-credential-68924"
RETURN_CANARY = "synthetic-fix4-return-credential-24061"
LOGGER = "axrm011.fix4.boundary"
app = Celery("axrm011-fix4-regressions", set_as_current=False)
app.conf.task_always_eager = True
app.conf.task_store_eager_result = False


class _EveryEnvelope(_CaptureTransport):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[bytes] = []
        self.item_types: list[str | None] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.payloads.append(envelope.serialize())
        self.item_types.extend(item.type for item in envelope.items)
        super().capture_envelope(envelope)


@contextmanager
def _sinks(
    monkeypatch: pytest.MonkeyPatch, *, logs: bool = False
) -> Iterator[tuple[logging.Logger, io.StringIO, sentry_sdk.Client, _EveryEnvelope]]:
    handler, stream = _production_console()
    logger = logging.getLogger(LOGGER)
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.INFO)
    transport = _EveryEnvelope()
    options = prod.SENTRY_OPTIONS.copy()
    options.update(
        {
            "dsn": "http://public@example.invalid/1",
            "transport": transport,
            "traces_sample_rate": 1.0,
            "enable_logs": logs,
        }
    )
    client = sentry_sdk.Client(**options)
    logger.addHandler(handler)
    try:
        with sentry_sdk.isolation_scope() as scope, sentry_sdk.new_scope():
            scope.clear()
            scope.set_client(client)
            yield logger, stream, client, transport
            client.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
        client.close()


def _recurse(secret: str, depth: int, strip_owner: bool = False) -> bool:
    if strip_owner and not depth:
        recursive.request.__dict__.pop(prod._TASK_KNOWN_OWNER, None)
    logging.getLogger(LOGGER).warning("argument echo %s", secret)
    sentry_sdk.capture_message("argument echo " + secret)
    if depth:
        recursive(INNER, depth - 1, strip_owner)
    return True


recursive = cast("Task", app.task(name="witness.fix4.same-task", lazy=False)(_recurse))


@pytest.mark.parametrize("strip_owner", [False, True], ids=["copied-owner", "no-owner"])
def test_a_cached_parent_request_cannot_hide_the_recursive_childs_arguments(
    monkeypatch: pytest.MonkeyPatch, strip_owner: bool
) -> None:
    # Given the real console and a client with all production options.
    with _sinks(monkeypatch) as (_, stream, client, transport):
        # When an observed request calls the same task with a different secret.
        result = recursive.apply(
            args=[CANARY, 1, strip_owner], task_id="fix4-recursion-90317"
        )
        client.flush()
        delivered = json.dumps(transport.events)
    # Then both requests were protected, and their values left with them.
    assert result.state == "SUCCESS"
    assert len(transport.events) >= 2
    assert all(
        secret not in stream.getvalue() + delivered for secret in (CANARY, INNER)
    )
    assert not prod._known_values()


def _record_dictionary(record: logging.LogRecord) -> dict[str, Any]:
    return cast(
        "dict[str, Any]", logging.LogRecord.__dict__["__dict__"].__get__(record)
    )


def _reject_dictionary(record: logging.LogRecord, value: dict[str, Any]) -> None:
    message = "whole dictionary replacement rejected"
    raise RuntimeError(message)


_LockedDictionary: type[logging.LogRecord] = type(
    "_LockedDictionary",
    (logging.LogRecord,),
    {"__dict__": property(_record_dictionary, _reject_dictionary)},
)


class _AttributeTrap(logging.LogRecord):
    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            message = "dictionary access rejected"
            raise RuntimeError(message)
        value: object = object.__getattribute__(self, name)
        return value


@pytest.mark.parametrize("kind", [_LockedDictionary, _AttributeTrap])
@pytest.mark.parametrize("logs", [False, True], ids=["events", "events-and-logs"])
def test_a_rejected_record_reaches_no_sdk_channel_or_later_breadcrumb(
    monkeypatch: pytest.MonkeyPatch, kind: type[logging.LogRecord], logs: bool
) -> None:
    # Given a record that cannot be safely mutated or formatted.
    record = kind(LOGGER, logging.ERROR, __file__, 0, CANARY, (), None)
    if kind is _LockedDictionary:
        descriptor = logging.LogRecord.__dict__["__dict__"]
        descriptor.__set__(record, _RejectUpdate(descriptor.__get__(record)))
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        # When Logger.handle invokes both the console and SDK's finally path.
        logger.handle(record)
        logger.error("ordinary following record")
        sentry_sdk.capture_message("later unrelated event")
        client.flush()
    # Then the control event and its breadcrumb flow, but the rejected record does not.
    emitted = stream.getvalue() + b"".join(transport.payloads).decode()
    assert CANARY not in emitted
    later = next(
        e for e in transport.events if e.get("message") == "later unrelated event"
    )
    assert "ordinary following record" in json.dumps(later.get("breadcrumbs"))
    assert "ordinary following record" in stream.getvalue()
    if logs:
        assert "log" in transport.item_types


def test_a_drop_is_irrevocable_even_if_a_later_scrub_would_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a record for which both scrubbing and withholding failed.
    record = logging.makeLogRecord(
        {"name": LOGGER, "levelno": logging.ERROR, "msg": CANARY, "exc_info": [None]}
    )

    def failing_withhold(*_args: object) -> None:
        message = "secondary failure"
        raise RuntimeError(message)

    with monkeypatch.context() as broken:
        broken.setattr(prod, "_withhold", failing_withhold)
        assert prod.CredentialURLLogFilter().filter(record) is False
    record.exc_info = None
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        # When the original is submitted again after its faults have disappeared.
        logger.handle(record)
        sentry_sdk.capture_message("later unrelated event")
        client.flush()
    # Then rejection still holds across console, events, breadcrumbs and SDK Logs.
    assert stream.getvalue() == ""
    assert CANARY not in b"".join(transport.payloads).decode()
    assert len(transport.events) == 1


class _RejectClear(dict[str, Any]):
    def clear(self) -> None:
        message = "secondary husk clear failure"
        raise RuntimeError(message)


class _RejectMarker(dict[str, Any]):
    def __setitem__(self, key: str, value: object) -> None:
        if key == prod._WITHHELD_EVENT:
            message = "secondary husk marker failure"
            raise RuntimeError(message)
        super().__setitem__(key, value)


@pytest.mark.parametrize("kind", [_RejectClear, _RejectMarker])
def test_secondary_husk_failures_do_not_escape_real_capture_or_revive_the_event(
    monkeypatch: pytest.MonkeyPatch, kind: type[dict[str, Any]]
) -> None:
    # Given a real SDK event whose fallback mapping methods also fail.
    event: Any = kind(message=CANARY, level="error")
    with _sinks(monkeypatch) as (_, _, client, transport):
        # When the raw SDK scrubber fails before before_send.
        with monkeypatch.context() as broken:
            broken.setattr(prod, "_task_stack", _MissingStack())
            sentry_sdk.capture_event(event)
        sentry_sdk.capture_message("ordinary control event")
        with sentry_sdk.start_transaction(name="ordinary-control", op="fix4"):
            pass
        client.flush()
    # Then no original is retained or sent, and ordinary events and transactions flow.
    assert CANARY not in repr(event) + b"".join(transport.payloads).decode()
    assert [e.get("type", "event") for e in transport.events] == [
        "event",
        "transaction",
    ]
    assert prod.SENTRY_OPTIONS["before_send"](event, {}) is None


def test_a_breadcrumb_is_scrubbed_before_its_known_values_leave_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a breadcrumb created in a transient credential context.
    with _sinks(monkeypatch) as (_, _, client, transport):
        with delivery_secrets(CANARY):
            sentry_sdk.add_breadcrumb(message=CANARY, category="transient-context")
        # When a later event is captured after that context has ended.
        sentry_sdk.capture_message("later unrelated event")
        client.flush()
    # Then the retained breadcrumb was already scrubbed at its ingress.
    assert len(transport.events) == 1
    assert CANARY not in json.dumps(transport.events)
    assert "transient-context" in json.dumps(transport.events[0].get("breadcrumbs"))


def test_a_private_stack_rename_does_not_crash_settings_import() -> None:
    # Given a fresh interpreter in which Celery's private stack name is unavailable.
    # When settings import, console output and the real SDK all run.
    result = subprocess.run(
        ["./.venv/bin/python", "tests/security/redaction_import_probe.py"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    # Then startup completes and the unavailable redaction context fails closed.
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["safe"] is True


@pytest.mark.parametrize(
    "hook", ["before_send", "before_send_transaction", "before_breadcrumb"]
)
def test_hook_mapping_failures_are_contained(
    hook: str,
) -> None:
    # Given a mapping with an overridable get that raises.
    event: Any = _RejectGet(message=CANARY)
    # When any outgoing or retention hook inspects it.
    result = prod.SENTRY_OPTIONS[hook](event, {})
    # Then the hook drops it without propagating the secondary failure.
    assert result is None


def test_rejected_record_hints_cannot_bypass_the_logging_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an unsupported record and SDK calls independent of Logger.handle.
    record = _LockedDictionary(LOGGER, logging.ERROR, __file__, 0, CANARY, (), None)
    with _sinks(monkeypatch) as (_, _, client, transport):
        # When events and breadcrumbs explicitly carry the rejected original hint.
        sentry_sdk.capture_event({"message": CANARY}, hint={"log_record": record})
        sentry_sdk.add_breadcrumb(message=CANARY, hint={"log_record": record})
        sentry_sdk.capture_message("later unrelated event")
        client.flush()
    # Then both hinted diagnostics were dropped before any later delivery.
    assert len(transport.events) == 1
    assert CANARY not in b"".join(transport.payloads).decode()


def _succeed(secret: str) -> str:
    return RETURN_CANARY


success_task = cast("Task", app.task(name="witness.fix4.success", lazy=False)(_succeed))


def test_success_identity_survives_without_disclosing_arguments_or_return_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the actual production formatter on Celery's success logger.
    handler, stream = _production_console()
    with _failure_logger(monkeypatch, handler):
        # When a successful task returns a different private provider result.
        result = success_task.apply(args=[CANARY], task_id="fix4-success-90317")
    # Then the task identity remains usable and the private result stays hidden.
    assert result.state == "SUCCESS"
    assert "witness.fix4.success" in stream.getvalue()
    assert "fix4-success-90317" in stream.getvalue()
    assert all(canary not in stream.getvalue() for canary in (CANARY, RETURN_CANARY))

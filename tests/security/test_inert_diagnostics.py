"""Production diagnostics must never execute the objects they describe."""

from __future__ import annotations

import json
import logging
import sys
import threading
from contextvars import copy_context
from functools import wraps
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest
import sentry_sdk
from celery import Celery
from sentry_sdk import logger as sdk_logger
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations import celery as sentry_celery
from sentry_sdk.integrations.django import _got_request_exception

from config.settings import prod
from tests.security.test_redaction_boundary_regressions import LOGGER, _sinks

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextvars import Context

    from sentry_sdk.types import Log

CANARY = "synthetic-fix6-tripwire-credential-824195"
OUTER = "ordinary fix6 outer diagnostic"
FOLLOWING = "ordinary fix6 following diagnostic"
RECORD_POSITIONS = (
    "message",
    "percent-s",
    "percent-r",
    "percent-format",
    "single-mapping",
    "extra-value",
    "extra-mapping",
    "request",
    "levelname",
    "stack_info",
    "exc_text",
)
EXCEPTION_POSITIONS = (
    "exception",
    "exception-args",
    "exception-notes",
    "exception-cause",
    "exception-context",
    "exception-group",
    "capture-exception",
    "django-integration",
    "celery-integration",
)
SDK_POSITIONS = (
    "event",
    "scope-extra",
    "scope-attribute",
    "breadcrumb-data",
    "sdk-log-attribute",
    "sdk-public-message",
    "sdk-public-parameter",
    "sdk-public-attribute",
    "sdk-public-attribute-mapping",
    "sdk-log-carrier",
    "breadcrumb-carrier",
)
POSITIONS = (*RECORD_POSITIONS, *EXCEPTION_POSITIONS, *SDK_POSITIONS)


def _safe_failure(function: Callable[..., None]) -> Callable[..., None]:
    @wraps(function)
    def run(*args: object, **kwargs: object) -> None:
        failure = ""
        try:
            function(*args, **kwargs)
        except BaseException as error:  # noqa: BLE001 - preserve failure without hostile context
            failure = type.__dict__["__name__"].__get__(type(error))
            trace = BaseException.__dict__["__traceback__"].__get__(error)
            while trace is not None:
                failure += f" {trace.tb_frame.f_code.co_name}:{trace.tb_lineno}"
                trace = trace.tb_next
        if failure:
            message = "diagnostic assertion or call failed: " + failure
            raise AssertionError(message) from None

    return run


def _tripwires(
    logger: logging.Logger, shape: str
) -> tuple[object, Exception, dict[str, object], list[str], list[Context]]:
    calls: list[str] = []
    saved: list[Context] = []

    def echo(depth: int = 0) -> None:
        logger.error("tripwire nested credential echo %s", CANARY)
        if depth:
            copy_context().run(echo, depth - 1)

    def trip(name: str, *_args: object, **_kwargs: object) -> NoReturn:
        calls.append(name)
        if len(calls) == 1:
            saved.append(copy_context())
            if shape == "thread":
                child = threading.Thread(target=echo)
                child.start()
                child.join(10)
                assert not child.is_alive()
            elif shape == "copied":
                saved[0].run(echo)
            elif shape == "nested":
                echo(2)
            else:
                echo()
        message = "application diagnostic hook executed"
        raise RuntimeError(message)

    def hook(name: str) -> Callable[..., NoReturn]:
        def called(*args: object, **kwargs: object) -> NoReturn:
            trip(name, *args, **kwargs)

        return called

    methods = {
        name: hook(name)
        for name in (
            "__getattribute__",
            "__getattr__",
            "__setattr__",
            "__str__",
            "__repr__",
            "__format__",
            "__hash__",
            "__eq__",
            "__bool__",
            "__len__",
            "__iter__",
            "__getitem__",
            "__int__",
            "__float__",
            "items",
            "keys",
            "get",
        )
    }
    kind = type("TripwireMeta", (type,), methods) if shape == "metaclass" else type
    opaque = kind("Tripwire", (), methods)()
    error = kind("TripwireError", (Exception,), methods)("ordinary primitive code")
    mapping = kind("TripwireMapping", (dict,), methods)({"password": CANARY})
    return opaque, error, mapping, calls, saved


def _emit_record(
    logger: logging.Logger, position: str, opaque: object, mapping: dict[str, object]
) -> None:
    extra: dict[str, object] = {"password": CANARY}
    if position == "message":
        logger.error(opaque, extra=extra)
    elif position.startswith("percent-"):
        template = {
            "percent-s": "%s",
            "percent-r": "%r",
            "percent-format": "%f",
        }[position]
        message = OUTER + " " + template
        logger.error(message, opaque, extra=extra)
    elif position == "single-mapping":
        message = OUTER + " %(password)s"
        logger.error(message, mapping, extra=extra)
    elif position == "extra-mapping":
        logger.error(OUTER, extra=mapping)
    elif position in {"extra-value", "request"}:
        name = "request" if position == "request" else "opaque"
        logger.error(OUTER, extra={**extra, name: opaque})
    else:
        record = logging.LogRecord(LOGGER, logging.ERROR, __file__, 0, OUTER, (), None)
        record.__dict__.update(extra)
        record.__dict__[position] = opaque
        logger.handle(record)


def _raise(exception: Exception) -> NoReturn:
    raise exception


def _emit_exception(
    logger: logging.Logger, position: str, opaque: object, error: Exception
) -> None:
    exception: Exception = error
    if position == "exception-args":
        exception = RuntimeError(opaque)
    elif position == "exception-group":
        exception = ExceptionGroup("ordinary group", [error])
    elif position in {"exception-notes", "exception-cause", "exception-context"}:
        exception = RuntimeError("ordinary primitive code")
        if position == "exception-notes":
            exception.__notes__ = cast("list[str]", [opaque])
        elif position == "exception-cause":
            exception.__cause__ = error
        else:
            exception.__context__ = error
    try:
        _raise(exception)
    except Exception:
        if position == "capture-exception":
            sentry_sdk.capture_exception(exception)
        elif position == "django-integration":
            _got_request_exception()
        elif position == "celery-integration":
            # Sentry's failure ingress, not Celery's accepted P3 renderer.
            app = Celery("fix6-tripwire", set_as_current=False)
            sentry_celery._capture_exception(app.Task(), sys.exc_info())
        else:
            logger.exception(OUTER, extra={"password": CANARY})


def _emit_public_sdk(position: str, opaque: object, mapping: dict[str, object]) -> None:
    attributes: dict[str, object] = {"password": CANARY}
    if position == "sdk-public-attribute":
        attributes["opaque"] = opaque
    elif position == "sdk-public-attribute-mapping":
        attributes = mapping
    if position == "sdk-public-message":
        sdk_logger.error(
            cast("str", opaque), scalar="ordinary control", attributes=attributes
        )
    elif position == "sdk-public-parameter":
        template = OUTER + " {opaque}"
        sdk_logger.error(template, opaque=opaque, attributes=attributes)
    else:
        sdk_logger.error(OUTER, attributes=attributes)


def _emit_sdk(
    position: str,
    opaque: object,
    client: sentry_sdk.Client,
    mapping: dict[str, object],
) -> None:
    extra = {"password": CANARY, "opaque": opaque}
    if position.startswith("sdk-public-"):
        _emit_public_sdk(position, opaque, mapping)
    elif position == "event":
        sentry_sdk.capture_event({"message": OUTER, "extra": extra})
    elif position == "scope-extra":
        sentry_sdk.set_extra("opaque", opaque)
        sentry_sdk.set_extra("password", CANARY)
        sentry_sdk.capture_message(OUTER)
    elif position == "scope-attribute":
        sentry_sdk.get_current_scope().set_attribute("opaque", cast("Any", opaque))
        sentry_sdk.get_current_scope().set_attribute("token", CANARY)
        sentry_sdk.capture_message(OUTER)
        logging.getLogger(LOGGER).error(OUTER)
    elif position == "breadcrumb-carrier":
        dict.clear(mapping)
        dict.update(mapping, {"message": OUTER, "data": extra})
        sentry_sdk.add_breadcrumb(mapping)
        sentry_sdk.capture_message(OUTER)
    elif position == "breadcrumb-data":
        sentry_sdk.add_breadcrumb(message=OUTER, data=extra)
        sentry_sdk.capture_message(OUTER)
    else:
        log: dict[str, object] = {
            "body": OUTER,
            "attributes": extra,
            "severity_text": "error",
            "severity_number": 17,
            "time_unix_nano": 0,
            "trace_id": None,
            "span_id": None,
        }
        if position == "sdk-log-carrier":
            dict.clear(mapping)
            dict.update(mapping, log)
            log = mapping
        client._capture_log(
            cast("Log", cast("object", log)), sentry_sdk.get_current_scope()
        )


@pytest.mark.parametrize("logs", [False, True], ids=["events", "events-and-logs"])
@pytest.mark.parametrize(
    "shape", ["direct", "copied", "thread", "nested", "accessor", "metaclass"]
)
@pytest.mark.parametrize("position", POSITIONS)
@_safe_failure
def test_no_application_method_runs_on_the_real_diagnostic_paths(
    monkeypatch: pytest.MonkeyPatch, position: str, shape: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, error, mapping, calls, saved = _tripwires(logger, shape)
        if position in RECORD_POSITIONS:
            _emit_record(logger, position, opaque, mapping)
        elif position in EXCEPTION_POSITIONS:
            _emit_exception(logger, position, opaque, error)
        else:
            _emit_sdk(position, opaque, client, mapping)
        known_after = prod._known_values()
        logger.error(FOLLOWING)
        sentry_sdk.capture_message(FOLLOWING)
        client.flush()
        assert calls == [], calls
        assert saved == []
    delivered = b"".join(transport.payloads).decode()
    assert CANARY not in stream.getvalue() + delivered + json.dumps(transport.events)
    assert "log record withheld" not in stream.getvalue()
    assert FOLLOWING in stream.getvalue()
    assert any(event.get("message") == FOLLOWING for event in transport.events)
    assert known_after == set()
    sdk_log = position.startswith("sdk-")
    if sdk_log and logs:
        assert "log" in transport.item_types
        items = [
            log
            for payload in transport.payloads
            for item in Envelope.deserialize(payload)
            if item.type == "log"
            for log in json.loads(item.payload.get_bytes())["items"]
        ]
        assert any(
            OUTER in item["body"]
            or (position == "sdk-public-message" and "Tripwire" in item["body"])
            for item in items
        )
    elif not sdk_log:
        assert any(
            event.get("message") == OUTER
            or OUTER in json.dumps(event.get("logentry"))
            or event.get("exception", {}).get("values")
            or (position == "message" and event.get("logentry"))
            for event in transport.events
        )


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(
    "shape", ["direct", "copied", "thread", "nested", "accessor", "metaclass"]
)
@_safe_failure
def test_unsupported_name_is_withheld_without_running_application_methods(
    monkeypatch: pytest.MonkeyPatch, shape: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, _, _, calls, saved = _tripwires(logger, shape)
        logger.handle(
            logging.makeLogRecord(
                {
                    "name": opaque,
                    "msg": OUTER,
                    "levelno": logging.ERROR,
                    "password": CANARY,
                }
            )
        )
        logger.error(FOLLOWING)
        sentry_sdk.capture_message(FOLLOWING)
        client.flush()
        assert calls == []
        assert saved == []
    assert prod.SENTRY_REDACTION_MARKER in stream.getvalue()
    assert FOLLOWING in stream.getvalue()
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert any(event.get("message") == FOLLOWING for event in transport.events)
    assert any(
        type(message := event.get("logentry", {}).get("message")) is str
        and prod.SENTRY_REDACTION_MARKER in message
        for event in transport.events
    )


@pytest.mark.parametrize("logs", [False, True])
def test_a_context_saved_by_a_sink_retains_no_record_credentials(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    saved: list[Context] = []

    class CopyingSink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            saved.append(copy_context())

    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        sink = CopyingSink()
        logger.addHandler(sink)
        try:
            logger.error(OUTER, extra={"password": CANARY})
        finally:
            logger.removeHandler(sink)
            sink.close()
        assert len(saved) == 1

        def next_request() -> set[str]:
            with sentry_sdk.isolation_scope() as scope, sentry_sdk.new_scope():
                scope.clear()
                scope.set_client(client)
                known = prod._known_values()
                logger.error("unrelated next request %s", CANARY)
                sentry_sdk.capture_message(FOLLOWING)
                return known

        assert saved[0].run(next_request) == set()
        client.flush()
    assert "unrelated next request " + CANARY in stream.getvalue()
    assert any(event.get("message") == FOLLOWING for event in transport.events)

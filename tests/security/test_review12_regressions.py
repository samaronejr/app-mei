"""Tripwires at every newly audited diagnostic ingress, using real sinks."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations import celery as sentry_celery

from apps.delivery.privacy import sensitive_values
from config.settings import prod
from tests.security.test_inert_diagnostics import _safe_failure
from tests.security.test_redaction_boundary_regressions import _EveryEnvelope, _sinks

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import StringIO

    from sentry_sdk.types import Event, Hint, Log

CANARY = "synthetic-fix7-dispatch-credential-741829"
OUTER = "ordinary fix7 outer diagnostic"
FOLLOWING = "ordinary fix7 following diagnostic"
APIS = ("capture_event", "capture_message", "capture_exception")
MAPPINGS = ("extras", "contexts", "tags", "attributes", "user")


def _hostiles(
    logger: logging.Logger,
) -> tuple[type, type, type, list[str], list[bool]]:
    calls: list[str] = []
    armed = [False]

    def trip(name: str) -> None:
        if armed[0]:
            calls.append(name)
            if len(calls) == 1:
                logger.error("fix7 nested credential echo %s", CANARY)

    def hook(name: str, native: Callable[..., object]) -> Callable[..., object]:
        def called(*args: object) -> object:
            trip(name)
            return native(*args)

        return called

    opaque = type(
        "Opaque",
        (),
        {
            name: hook(name, cast("Callable[..., object]", native))
            for name, native in {
                "__getattribute__": object.__getattribute__,
                "__str__": lambda _self: "ordinary opaque",
                "__repr__": lambda _self: "ordinary opaque",
                "__format__": lambda _self, _spec: "ordinary opaque",
                "__eq__": lambda _self, _other: False,
                "__hash__": lambda _self: 17,
                "__bool__": lambda _self: True,
                "__len__": lambda _self: 1,
                "__iter__": lambda _self: iter(()),
                "__getitem__": lambda _self, _key: "ordinary item",
                "__add__": lambda _self, _other: 1,
                "__call__": lambda _self, _scope: None,
            }.items()
        },
    )
    name = type(
        "Name",
        (str,),
        {
            "__slots__": (),
            **{
                method: hook("name." + method, cast("Callable[..., object]", native))
                for method, native in {
                    "__str__": str.__str__,
                    "__repr__": str.__repr__,
                    "__format__": lambda self, _spec: str.__str__(self),
                    "__hash__": str.__hash__,
                    "__eq__": str.__eq__,
                }.items()
            },
        },
    )
    mapping = type(
        "Mapping",
        (dict,),
        {
            method: hook("mapping." + method, cast("Callable[..., object]", native))
            for method, native in {
                "__iter__": dict.__iter__,
                "keys": dict.keys,
                "items": dict.items,
                "__getitem__": dict.__getitem__,
                "__len__": dict.__len__,
                "__bool__": lambda _self: True,
            }.items()
        },
    )
    return opaque, name, mapping, calls, armed


def _assert_sinks(
    stream: StringIO,
    client: sentry_sdk.Client,
    transport: _EveryEnvelope,
    calls: list[str],
) -> None:
    sentry_sdk.capture_message(FOLLOWING)
    client.flush()
    logs = [
        entry
        for data in transport.payloads
        for item in Envelope.deserialize(data)
        if item.type == "log"
        for entry in json.loads(item.payload.get_bytes())["items"]
    ]
    surfaces = {
        "console": stream.getvalue(),
        "events": json.dumps(transport.events),
        "envelopes": b"".join(transport.payloads).decode(),
        "breadcrumbs": json.dumps(
            [event.get("breadcrumbs") for event in transport.events]
        ),
        "sdk_logs": json.dumps(logs),
    }
    assert calls == [], calls
    assert all(CANARY not in text for text in surfaces.values()), [
        name for name, text in surfaces.items() if CANARY in text
    ]
    assert any(event.get("message") == FOLLOWING for event in transport.events)
    assert prod._known_values() == set()


def _capture(api: str, **kwargs: object) -> None:
    options = cast("dict[str, Any]", kwargs)
    if api == "capture_event":
        sentry_sdk.capture_event({"message": OUTER}, **options)
    elif api == "capture_message":
        sentry_sdk.capture_message(OUTER, **options)
    else:
        sentry_sdk.capture_exception(RuntimeError(OUTER), **options)


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("position", ["argument", "exception", "capture-exception"])
@_safe_failure
def test_descriptor_returned_class_names_are_inert(
    monkeypatch: pytest.MonkeyPatch, position: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, name, _, calls, arm = _hostiles(logger)
        error = type("PrimitiveCodeError", (RuntimeError,), {})
        opaque.__name__ = name("Opaque")
        error.__name__ = name("PrimitiveCodeError")
        arm[0] = True
        if position == "argument":
            message = OUTER + " %s"
            logger.error(message, opaque(), extra={"password": CANARY})
        elif position == "exception":
            logger.error(
                OUTER,
                exc_info=(error, error("ordinary_code"), None),
                extra={"password": CANARY},
            )
        else:
            sentry_sdk.capture_exception(
                error("ordinary_code"), extras={"password": CANARY}
            )
        _assert_sinks(stream, client, transport, calls)
        assert any(
            OUTER in json.dumps(event) or "PrimitiveCodeError" in json.dumps(event)
            for event in transport.events
        )


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("kind", ["opaque", "string-subclass"])
@_safe_failure
def test_event_control_type_is_owned_before_comparison(
    monkeypatch: pytest.MonkeyPatch, kind: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, name, _, calls, arm = _hostiles(logger)
        event_type = opaque() if kind == "opaque" else name("ordinary_event")
        arm[0] = True
        sentry_sdk.capture_event(
            {"type": event_type, "message": OUTER, "extra": {"password": CANARY}}
        )
        _assert_sinks(stream, client, transport, calls)
        assert any(event.get("message") == OUTER for event in transport.events)


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("field", MAPPINGS)
@_safe_failure
def test_every_capture_mapping_keyword_is_owned_before_sdk_merge(
    monkeypatch: pytest.MonkeyPatch, api: str, field: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        _, _, mapping, calls, arm = _hostiles(logger)
        value = mapping(password=CANARY)
        if field == "contexts":
            value = mapping(ordinary={"password": CANARY})
        arm[0] = True
        _capture(api, **{field: value})
        _assert_sinks(stream, client, transport, calls)
        assert any(OUTER in json.dumps(event) for event in transport.events)


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("field", ["fingerprint", "level"])
@_safe_failure
def test_capture_scalar_keywords_never_reach_sdk_protocols(
    monkeypatch: pytest.MonkeyPatch, api: str, field: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, _, _, calls, arm = _hostiles(logger)
        arm[0] = True
        _capture(api, **{field: opaque(), "extras": {"password": CANARY}})
        _assert_sinks(stream, client, transport, calls)


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "kind",
    [
        "callable",
        "scope-subclass",
        "native-extras",
        "native-contexts",
        "native-tags",
        "native-attributes",
        "native-user",
        "native-transaction_info",
    ],
)
@_safe_failure
def test_explicit_scope_never_dispatches_to_application_objects(
    monkeypatch: pytest.MonkeyPatch, api: str, kind: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, _, mapping, calls, arm = _hostiles(logger)
        if kind == "callable":
            scope = opaque()
        elif kind == "scope-subclass":
            scope = type(
                "ApplicationScope",
                (sentry_sdk.Scope,),
                {"__getattribute__": opaque.__dict__["__getattribute__"]},
            )()
        else:
            scope = sentry_sdk.Scope()
            value = mapping(password=CANARY)
            if kind == "native-contexts":
                value = mapping(ordinary={"password": CANARY})
            elif kind == "native-attributes":
                value = mapping(password={"value": CANARY, "type": "string"})
            object.__setattr__(scope, "_" + kind.removeprefix("native-"), value)
        arm[0] = True
        _capture(api, scope=scope)
        arm[0] = False
        _assert_sinks(stream, client, transport, calls)
        if kind.startswith("native-"):
            assert any(OUTER in json.dumps(event) for event in transport.events)


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("field", ["args", "level", "stack_info", "stacklevel"])
@_safe_failure
def test_record_creation_control_fields_are_exact_native_values(
    monkeypatch: pytest.MonkeyPatch, field: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, _, _, calls, arm = _hostiles(logger)
        options: dict[str, Any] = {
            "level": logging.ERROR,
            "msg": OUTER,
            "args": (),
            "extra": {"password": CANARY},
        }
        options[field] = opaque()
        arm[0] = True
        logger._log(**options)
        _assert_sinks(stream, client, transport, calls)
        assert OUTER in stream.getvalue() or "[Filtered]" in stream.getvalue()


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_delivery_context_string_subclasses_are_not_hashed(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        _, name, _, calls, arm = _hostiles(logger)
        value = name(CANARY)
        token = sensitive_values.set((value,))
        try:
            arm[0] = True
            message = OUTER + " %s"
            logger.error(message, CANARY)
        finally:
            sensitive_values.reset(token)
        _assert_sinks(stream, client, transport, calls)
        assert OUTER in stream.getvalue()


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_task_exception_policy_reads_only_exact_namespace_keys(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        _, name, _, calls, arm = _hostiles(logger)
        task = type("TaskMetadata", (), {name("throws"): ()})()
        error = RuntimeError(OUTER)
        arm[0] = True
        sentry_celery._capture_exception(task, (RuntimeError, error, None))
        _assert_sinks(stream, client, transport, calls)
        assert any(OUTER in json.dumps(event) for event in transport.events)


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(
    "position", ["record-extra", "record-argument", "event-root", "scope-extras"]
)
@_safe_failure
def test_native_text_storage_preserves_sensitive_subclass_field_names(
    monkeypatch: pytest.MonkeyPatch, position: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        _, name, _, calls, arm = _hostiles(logger)
        private = {name("password"): CANARY, "echo": CANARY}
        event = {name("extra"): private, "message": OUTER + " " + CANARY}
        arm[0] = True
        if position == "record-extra":
            logger.error(OUTER, extra=private)
        elif position == "record-argument":
            message = OUTER + " %s"
            logger.error(message, private)
        elif position == "event-root":
            sentry_sdk.capture_event(cast("Event", cast("object", event)))
        else:
            sentry_sdk.capture_message(OUTER + " " + CANARY, extras=private)
        _assert_sinks(stream, client, transport, calls)
        assert any(OUTER in json.dumps(event) for event in transport.events)


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_native_bytes_storage_preserves_sensitive_subclass_field_names(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        _, _, _, calls, arm = _hostiles(logger)

        class ByteName(bytes):
            __slots__ = ()

            def __hash__(self) -> int:
                if arm[0]:
                    calls.append("bytes.__hash__")
                    logger.error("fix7 nested credential echo %s", CANARY)
                return bytes.__hash__(self)

            def __repr__(self) -> str:
                if arm[0]:
                    calls.append("bytes.__repr__")
                    logger.error("fix7 nested credential echo %s", CANARY)
                return bytes.__repr__(self)

        private = {ByteName(b"password"): CANARY, "echo": CANARY}
        arm[0] = True
        message = OUTER + " %s"
        logger.error(message, private)
        _assert_sinks(stream, client, transport, calls)
        assert OUTER in stream.getvalue()


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_sdk_processors_receive_only_an_inert_hinted_record(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        opaque, _, _, calls, arm = _hostiles(logger)
        record = logging.LogRecord(
            logger.name, logging.ERROR, __file__, 0, opaque(), (), None
        )
        record.__dict__["password"] = CANARY

        def render_hint(event: Event, hint: Hint) -> Event:
            if "log_record" in hint:
                hint["log_record"].getMessage()
            return event

        sentry_sdk.get_current_scope().add_event_processor(render_hint)
        arm[0] = True
        sentry_sdk.capture_event(
            {"message": OUTER, "extra": {"password": CANARY}},
            hint={"log_record": record},
        )
        _assert_sinks(stream, client, transport, calls)
        assert any(event.get("message") == OUTER for event in transport.events)


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_direct_sdk_logs_own_scope_attributes_before_the_sdk_iterates(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        _, _, mapping, calls, arm = _hostiles(logger)
        scope = sentry_sdk.Scope()
        scope._attributes = mapping(password={"value": CANARY, "type": "string"})
        log = {
            "body": OUTER,
            "attributes": {},
            "severity_text": "error",
            "severity_number": 17,
            "time_unix_nano": 0,
            "trace_id": None,
            "span_id": None,
        }
        arm[0] = True
        client._capture_log(cast("Log", cast("object", log)), scope)
        _assert_sinks(stream, client, transport, calls)
        if logs:
            assert OUTER in b"".join(transport.payloads).decode()


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_record_header_lookup_never_compares_original_subclass_keys(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        _, name, _, calls, arm = _hostiles(logger)
        record = logging.LogRecord(
            logger.name, logging.ERROR, __file__, 0, OUTER, (), None
        )
        record.__dict__["password"] = CANARY
        record.__dict__ = {name(key): value for key, value in record.__dict__.items()}
        arm[0] = True
        logger.handle(record)
        _assert_sinks(stream, client, transport, calls)
        assert OUTER in stream.getvalue()

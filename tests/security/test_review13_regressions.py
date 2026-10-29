"""Scope state read at capture never runs application code, explicit or ambient."""

from __future__ import annotations

import json
import logging
from collections import deque
from enum import IntEnum
from typing import TYPE_CHECKING, Any, cast

import pytest
import sentry_sdk
from sentry_sdk import logger as sdk_logger
from sentry_sdk.client import NonRecordingClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.feature_flags import FlagBuffer, add_feature_flag
from sentry_sdk.session import Session
from sentry_sdk.tracing import Span, Transaction
from sentry_sdk.tracing_utils import Baggage, PropagationContext

from config.settings import prod
from tests.security.test_inert_diagnostics import _safe_failure
from tests.security.test_redaction_boundary_regressions import _sinks

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import StringIO

    from sentry_sdk.types import Event, Log

    from tests.security.test_redaction_boundary_regressions import _EveryEnvelope

CANARY = "synthetic-fix8-scope-credential-741829"
OUTER = "ordinary fix8 outer diagnostic"
FOLLOWING = "ordinary fix8 following diagnostic"
RECORD = OUTER + " record"
TRACED_RECORD = OUTER + " traced record"
WITHHELD = "[Filtered] log record withheld: redaction failed"
SOURCES = ("explicit", "isolation", "current", "global")
APIS = ("capture_event", "capture_message", "capture_exception")
# The type each Scope slot holds when the SDK fills it, so every slot can carry a
# hostile subclass. Keyed by the SDK's own slot list, not the boundary allowlist.
SLOT_TYPES: dict[str, type | None] = {
    "_level": str,
    "_name": str,
    "_fingerprint": list,
    "_transaction": str,
    "_transaction_info": dict,
    "_user": dict,
    "_tags": dict,
    "_contexts": dict,
    "_extras": dict,
    "_breadcrumbs": deque,
    "_n_breadcrumbs_truncated": int,
    "_gen_ai_original_message_count": dict,
    "_gen_ai_conversation_id": str,
    "_event_processors": list,
    "_error_processors": list,
    "_should_capture": None,
    "_span": Span,
    "_session": Session,
    "_attachments": list,
    "_force_auto_session_tracking": None,
    "_profile": None,
    "_propagation_context": PropagationContext,
    "client": NonRecordingClient,
    "_type": None,
    "_last_event_id": str,
    "_flags": FlagBuffer,
    "_attributes": dict,
}
# Processors are configured code the SDK calls by design; only their list is data.
PROCESSORS = ("_event_processors", "_error_processors")
HOOKS: dict[str, Callable[..., object]] = {
    "__getattribute__": object.__getattribute__,
    "__bool__": lambda _self: True,
    "__len__": lambda _self: 1,
    "__iter__": lambda _self: iter(()),
    "__contains__": lambda _self, _item: False,
    "__getitem__": lambda _self, _key: "ordinary item",
    "__eq__": lambda _self, _other: False,
    "__hash__": lambda _self: 17,
    "__lt__": lambda _self, _other: False,
    "__gt__": lambda _self, _other: False,
    "__add__": lambda _self, _other: 1,
    "__radd__": lambda _self, _other: 1,
    "__index__": lambda _self: 1,
    "__str__": lambda _self: "ordinary opaque",
    "__repr__": lambda _self: "ordinary opaque",
    "__format__": lambda _self, _spec: "ordinary opaque",
    "__call__": lambda *_args: None,
    "__copy__": lambda self: self,
    "__deepcopy__": lambda self, _memo: self,
    "copy": lambda self: self,
    "keys": lambda _self: [],
    "items": lambda _self: [],
    "get": lambda _self, *_args: None,
    "append": lambda _self, _item: None,
    "extend": lambda _self, _items: None,
}


class _Tripwire:
    """Record every hook call while armed; the first one echoes a credential."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.calls: list[str] = []
        self.armed = False

    def trip(self, name: str) -> None:
        if self.armed:
            self.calls.append(name)
            if len(self.calls) == 1:
                self.logger.error("fix8 nested credential echo %s", CANARY)

    def hooked(self, name: str, base: type) -> type:
        def method(hook: str, native: Callable[..., object]) -> Callable[..., object]:
            def called(*args: object) -> object:
                self.trip(name + "." + hook)
                return native(*args)

            return called

        namespace = {
            hook: method(hook, getattr(base, hook, None) or fallback)
            for hook, fallback in HOOKS.items()
            if base is object or getattr(base, hook, None) is not None
        }
        return type("Hostile" + base.__name__.title(), (base,), namespace)

    def value(self, slot: str, kind: str) -> object:
        if kind == "opaque":
            return self.hooked(slot, object)()
        base = cast("type", SLOT_TYPES[slot])
        if kind == "item":
            if base is deque:
                return deque([self.hooked(slot, dict)(_crumb())])
            item = self.hooked(slot, object)()
            return {"password": CANARY, "hostile": item} if base is dict else [item]
        contents: dict[type, tuple[object, ...]] = {
            dict: (_mapping(slot),),
            deque: ([_crumb()],),
            list: (["fix8"] if slot == "_fingerprint" else [],),
            int: (1,),
            str: ("ordinary",),
            FlagBuffer: (100,),
        }
        return self.hooked(slot, base)(*contents.get(base, ()))


def _crumb() -> dict[str, object]:
    return {
        "type": "default",
        "message": "fix8 retained crumb",
        "timestamp": "2026-10-29T00:00:00+00:00",
        "data": {"password": CANARY},
    }


def _mapping(slot: str) -> dict[str, object]:
    if slot == "_contexts":
        return {"fix8": {"password": CANARY}}
    if slot == "_attributes":
        return {"password": {"value": CANARY, "type": "string"}}
    if slot == "_gen_ai_original_message_count":
        return {"fix8-span": 1}
    if slot == "_transaction_info":
        return {"source": "custom"}
    return {"password": CANARY, "ordinary": "fix8 value"}


def _cases() -> list[tuple[str, str, str]]:
    cases: list[tuple[str, str, str]] = []
    for source in SOURCES:
        for slot in sentry_sdk.Scope.__slots__:
            # The SDK selects its client from the ambient scopes before any
            # boundary: replacing it there replaces the SDK, not a diagnostic.
            if slot == "client" and source != "explicit":
                continue
            kinds = ["opaque"]
            if SLOT_TYPES[slot] is not None:
                kinds.append("subclass")
            if SLOT_TYPES[slot] in {dict, deque, list} and slot not in PROCESSORS:
                kinds.append("item")
            cases.extend((source, slot, kind) for kind in kinds)
    return cases


def _target(source: str) -> sentry_sdk.Scope:
    if source == "explicit":
        return sentry_sdk.Scope()
    if source == "isolation":
        return sentry_sdk.get_isolation_scope()
    if source == "current":
        return sentry_sdk.get_current_scope()
    return sentry_sdk.get_global_scope()


def _log(message: str) -> Log:
    return cast(
        "Log",
        {
            "body": message,
            "attributes": {},
            "severity_text": "error",
            "severity_number": 17,
            "time_unix_nano": 0,
            "trace_id": None,
            "span_id": None,
        },
    )


def _entries(
    source: str,
    target: sentry_sdk.Scope,
    logger: logging.Logger,
    client: sentry_sdk.Client,
) -> None:
    """Every capture entry point that reads this source's scope state."""
    explicit: dict[str, Any] = {"scope": target} if source == "explicit" else {}
    sentry_sdk.capture_event(
        {"message": OUTER + " event", "extra": {"password": CANARY}}, **explicit
    )
    sentry_sdk.capture_message(OUTER + " message", **explicit)
    sentry_sdk.capture_exception(RuntimeError(OUTER + " exception"), **explicit)
    if source == "explicit":
        client.capture_event(
            cast("Event", {"message": OUTER + " client"}), scope=target
        )
        client._capture_log(_log(OUTER + " client log"), target)
    else:
        logger.error(RECORD, extra={"password": CANARY})
        sdk_logger.error(OUTER + " sdk log")


def _sink_texts(
    stream: StringIO, transport: _EveryEnvelope
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    logs = [
        entry
        for data in transport.payloads
        for item in Envelope.deserialize(data)
        if item.type == "log"
        for entry in json.loads(item.payload.get_bytes())["items"]
    ]
    return {
        "console": stream.getvalue(),
        "events": json.dumps(transport.events),
        "envelopes": b"".join(transport.payloads).decode(errors="replace"),
        "breadcrumbs": json.dumps(
            [event.get("breadcrumbs") for event in transport.events]
        ),
        "sdk_logs": json.dumps(logs),
    }, logs


def _assert_clean(
    stream: StringIO,
    client: sentry_sdk.Client,
    transport: _EveryEnvelope,
    calls: list[str],
) -> None:
    sentry_sdk.capture_message(FOLLOWING)
    client.flush()
    surfaces, _ = _sink_texts(stream, transport)
    assert calls == [], calls
    assert all(CANARY not in text for text in surfaces.values()), [
        name for name, text in surfaces.items() if CANARY in text
    ]
    assert any(event.get("message") == FOLLOWING for event in transport.events)
    assert prod._known_values() == set()


def _crumbs(event: object) -> list[str]:
    values = (cast("dict[str, Any]", event).get("breadcrumbs") or {}).get("values", [])
    return [str(crumb.get("message")) for crumb in values]


def _messages(transport: _EveryEnvelope) -> set[str]:
    texts = set()
    for event in transport.events:
        texts.add(str(event.get("message")))
        texts.add(str((event.get("logentry") or {}).get("message")))
        texts.update(
            str(value.get("value"))
            for value in (event.get("exception") or {}).get("values", [])
        )
    return texts


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(("source", "slot", "kind"), _cases())
@_safe_failure
def test_every_scope_slot_is_inert_at_capture(
    monkeypatch: pytest.MonkeyPatch, source: str, slot: str, kind: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        value = wire.value(slot, kind)
        target = _target(source)
        storage = sentry_sdk.Scope.__dict__[slot]
        saved = storage.__get__(target)
        storage.__set__(target, value)
        wire.armed = True
        try:
            _entries(source, target, logger, client)
        finally:
            wire.armed = False
            storage.__set__(target, saved)
        _assert_clean(stream, client, transport, wire.calls)
        if kind != "opaque" and slot not in PROCESSORS:
            # Native storage of a replaced container is still read, not refused.
            delivered = _messages(transport)
            assert {OUTER + " event", OUTER + " message", OUTER + " exception"} <= (
                delivered
            )


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("api", APIS)
@_safe_failure
def test_review13_explicit_scope_breadcrumb_repro(
    monkeypatch: pytest.MonkeyPatch, api: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        calls: list[str] = []
        armed = [False]

        class Crumbs(deque[object]):
            def __bool__(self) -> bool:
                if armed[0]:
                    calls.append("scope.breadcrumbs.__bool__")
                    if len(calls) == 1:
                        logger.error("rv13 nested credential echo %s", CANARY)
                return True

        scope = sentry_sdk.Scope()
        object.__setattr__(scope, "_breadcrumbs", Crumbs([_crumb()]))
        armed[0] = True
        if api == "capture_event":
            sentry_sdk.capture_event(
                {"message": OUTER, "extra": {"password": CANARY}}, scope=scope
            )
        elif api == "capture_message":
            sentry_sdk.capture_message(OUTER, scope=scope)
        else:
            sentry_sdk.capture_exception(RuntimeError(OUTER), scope=scope)
        armed[0] = False
        _assert_clean(stream, client, transport, calls)
        assert OUTER in _messages(transport)
        assert any(
            "fix8 retained crumb" in _crumbs(event) for event in transport.events
        )


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("entry", [*APIS, "logger.error"])
@_safe_failure
def test_review13_ambient_scope_breadcrumb_repro(
    monkeypatch: pytest.MonkeyPatch, entry: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        calls: list[str] = []
        armed = [False]

        class Crumbs(deque[object]):
            def __bool__(self) -> bool:
                if armed[0]:
                    calls.append("isolation.breadcrumbs.__bool__")
                    if len(calls) == 1:
                        logger.error("rv13 nested credential echo %s", CANARY)
                return True

        isolation = sentry_sdk.get_isolation_scope()
        saved = isolation._breadcrumbs
        object.__setattr__(isolation, "_breadcrumbs", Crumbs([_crumb()]))
        armed[0] = True
        try:
            if entry == "capture_event":
                sentry_sdk.capture_event(
                    {"message": OUTER, "extra": {"password": CANARY}}
                )
            elif entry == "capture_message":
                sentry_sdk.capture_message(OUTER)
            elif entry == "capture_exception":
                sentry_sdk.capture_exception(RuntimeError(OUTER))
            else:
                logger.error(OUTER, extra={"password": CANARY})
        finally:
            armed[0] = False
            object.__setattr__(isolation, "_breadcrumbs", saved)
        _assert_clean(stream, client, transport, calls)
        assert OUTER in _messages(transport)


@pytest.mark.parametrize("api", APIS)
@_safe_failure
def test_an_explicit_scope_is_read_but_never_modified(
    monkeypatch: pytest.MonkeyPatch, api: str
) -> None:
    with _sinks(monkeypatch) as (_, stream, client, transport):
        scope = sentry_sdk.Scope()
        scope.set_extra("password", CANARY)
        scope.set_tag("ordinary", "fix8 tag")
        scope.set_context("fix8", {"password": CANARY})
        scope.set_user({"id": "fix8-user"})
        scope.add_breadcrumb({"message": "fix8 explicit crumb"})
        before = {
            name: sentry_sdk.Scope.__dict__[name].__get__(scope)
            for name in sentry_sdk.Scope.__slots__
        }
        copies = {
            name: (dict(value) if type(value) is dict else list(value))
            for name, value in before.items()
            if type(value) in {dict, list, deque}
        }
        if api == "capture_event":
            sentry_sdk.capture_event({"message": OUTER}, scope=scope)
        elif api == "capture_message":
            sentry_sdk.capture_message(OUTER, scope=scope)
        else:
            sentry_sdk.capture_exception(RuntimeError(OUTER), scope=scope)
        for name, value in before.items():
            assert sentry_sdk.Scope.__dict__[name].__get__(scope) is value, name
        for name, copied in copies.items():
            current = before[name]
            assert (dict(current) if type(current) is dict else list(current)) == (
                copied
            ), name
        _assert_clean(stream, client, transport, [])
        event = next(event for event in transport.events if OUTER in json.dumps(event))
        assert event["tags"]["ordinary"] == "fix8 tag"
        assert event["user"]["id"] == "fix8-user"
        assert event["extra"]["password"] == prod.SENTRY_REDACTION_MARKER


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("source", ["explicit", "isolation"])
@pytest.mark.parametrize("payload", ["bytes", "callable"])
@_safe_failure
def test_scope_attachments_never_reach_the_transport(
    monkeypatch: pytest.MonkeyPatch, payload: str, source: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)

        def body() -> bytes:
            wire.trip("attachment.bytes")
            return CANARY.encode()

        target = _target(source)
        saved = list(target._attachments)
        target.add_attachment(
            bytes=body if payload == "callable" else CANARY.encode(),
            filename="fix8-private.txt",
        )
        wire.armed = True
        try:
            if source == "explicit":
                sentry_sdk.capture_message(OUTER, scope=target)
            else:
                sentry_sdk.capture_message(OUTER)
        finally:
            wire.armed = False
            target._attachments = saved
        _assert_clean(stream, client, transport, wire.calls)
        assert OUTER in _messages(transport)
        assert "attachment" not in transport.item_types


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_merged_scope_keeps_sdk_precedence_and_ordinary_data(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (_, stream, client, transport):
        global_scope = sentry_sdk.get_global_scope()
        monkeypatch.setattr(global_scope, "_tags", {"layer": "global", "only": "g"})
        monkeypatch.setattr(global_scope, "_breadcrumbs", deque([{"message": "c0"}]))
        isolation = sentry_sdk.get_isolation_scope()
        isolation.set_tag("layer", "isolation")
        isolation.set_user({"id": "fix8-user"})
        isolation.set_extra("request_id", "fix8-request")
        sentry_sdk.add_breadcrumb({"message": "c1"})
        current = sentry_sdk.get_current_scope()
        current.set_tag("layer", "current")
        current.set_level("warning")
        current.set_context("fix8", {"stage": "merge"})
        current.fingerprint = ["fix8-fingerprint"]
        current.set_transaction_name("fix8-transaction", source="custom")
        isolation.set_attribute("fix8.scope_attribute", "ordinary")
        explicit = sentry_sdk.Scope()
        explicit.set_tag("layer", "explicit")
        explicit.add_breadcrumb({"message": "c2"})
        sentry_sdk.capture_message(OUTER + " ambient")
        sentry_sdk.capture_message(OUTER + " explicit", scope=explicit)
        sentry_sdk.capture_message(OUTER + " keyword", tags={"layer": "keyword"})
        sdk_logger.error(OUTER + " log")
        _assert_clean(stream, client, transport, [])
        events = {
            event["message"]: event
            for event in transport.events
            if str(event.get("message")).startswith(OUTER)
        }
        ambient = events[OUTER + " ambient"]
        assert ambient["tags"]["layer"] == "current"
        assert ambient["tags"]["only"] == "g"
        assert ambient["user"]["id"] == "fix8-user"
        assert ambient["extra"]["request_id"] == "fix8-request"
        assert ambient["level"] == "warning"
        assert ambient["contexts"]["fix8"] == {"stage": "merge"}
        assert ambient["fingerprint"] == ["fix8-fingerprint"]
        assert ambient["transaction"] == "fix8-transaction"
        assert ambient["transaction_info"] == {"source": "custom"}
        assert _crumbs(ambient)[:2] == ["c0", "c1"]
        assert events[OUTER + " explicit"]["tags"]["layer"] == "explicit"
        assert _crumbs(events[OUTER + " explicit"])[-1] == "c2"
        assert events[OUTER + " keyword"]["tags"]["layer"] == "keyword"
        if logs:
            _, entries = _sink_texts(stream, transport)
            assert any(
                entry["body"] == OUTER + " log"
                and entry["attributes"]["fix8.scope_attribute"]["value"] == "ordinary"
                for entry in entries
            )


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("source", SOURCES)
@_safe_failure
def test_configured_processors_receive_only_projected_scope_data(
    monkeypatch: pytest.MonkeyPatch, source: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        target = _target(source)
        planted = {
            "_level": wire.value("_level", "opaque"),
            "_transaction": wire.value("_transaction", "opaque"),
            "_fingerprint": wire.value("_fingerprint", "item"),
            "_user": wire.value("_user", "item"),
            "_extras": wire.value("_extras", "item"),
            "_tags": wire.value("_tags", "item"),
            "_contexts": {"fix8": {"hostile": wire.value("_contexts", "opaque")}},
            "_transaction_info": wire.value("_transaction_info", "item"),
        }
        saved = {
            name: sentry_sdk.Scope.__dict__[name].__get__(target) for name in planted
        }
        rendered: list[str] = []

        def render(event: Event, _hint: object) -> Event:
            # A configured processor may render whatever event data it is given.
            rendered.append(repr(event))
            return event

        sentry_sdk.get_isolation_scope().add_event_processor(render)
        for name, value in planted.items():
            sentry_sdk.Scope.__dict__[name].__set__(target, value)
        wire.armed = True
        try:
            if source == "explicit":
                sentry_sdk.capture_message(OUTER, scope=target)
            else:
                sentry_sdk.capture_message(OUTER)
        finally:
            wire.armed = False
            for name, value in saved.items():
                sentry_sdk.Scope.__dict__[name].__set__(target, value)
        _assert_clean(stream, client, transport, wire.calls)
        assert rendered
        assert OUTER in _messages(transport)


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("source", SOURCES)
@_safe_failure
def test_a_scope_that_cannot_be_owned_withholds_the_whole_capture(
    monkeypatch: pytest.MonkeyPatch, source: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        target = _target(source)
        echo = "fix8 refused echo " + CANARY
        # The scope labels a credential the diagnostic repeats unlabelled, and
        # another of its slots cannot be owned: sending the diagnostic without
        # the scope would drop the label, so the whole capture is withheld.
        planted = {
            "_extras": {"password": CANARY},
            "_tags": wire.value("_tags", "opaque"),
        }
        saved = {
            name: sentry_sdk.Scope.__dict__[name].__get__(target) for name in planted
        }
        for name, value in planted.items():
            sentry_sdk.Scope.__dict__[name].__set__(target, value)
        wire.armed = True
        try:
            if source == "explicit":
                sentry_sdk.capture_message(echo, scope=target)
                client.capture_event(cast("Event", {"message": echo}), scope=target)
                client._capture_log(_log(echo), target)
            else:
                sentry_sdk.capture_message(echo)
                sdk_logger.error(echo)
        finally:
            wire.armed = False
            for name, value in saved.items():
                sentry_sdk.Scope.__dict__[name].__set__(target, value)
        _assert_clean(stream, client, transport, wire.calls)
        assert "fix8 refused echo" not in json.dumps(transport.events)
        assert "fix8 refused echo" not in _sink_texts(stream, transport)[0]["sdk_logs"]


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_trace_linkage_and_release_health_survive_the_owned_merge(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        isolation = sentry_sdk.get_isolation_scope()
        isolation.start_session()
        session = isolation._session
        sentry_sdk.capture_message(OUTER + " untraced one")
        sentry_sdk.capture_message(OUTER + " untraced two")
        with (
            sentry_sdk.start_transaction(name="fix8-transaction") as transaction,
            sentry_sdk.start_span(op="fix8.child") as span,
        ):
            sentry_sdk.capture_exception(RuntimeError(OUTER + " traced"))
            logger.error(TRACED_RECORD)
        isolation._session = None
        _assert_clean(stream, client, transport, [])
        traces = {
            next(iter(_messages_of(event))): event["contexts"]["trace"]
            for event in transport.events
            if event.get("type") != "transaction" and _messages_of(event)
        }
        one = traces[OUTER + " untraced one"]
        assert one["trace_id"] == traces[OUTER + " untraced two"]["trace_id"]
        traced = traces[OUTER + " traced"]
        assert traced["trace_id"] == transaction.trace_id
        assert traced["span_id"] == span.span_id
        assert any(
            event.get("type") == "transaction"
            and event["contexts"]["trace"]["trace_id"] == transaction.trace_id
            for event in transport.events
        )
        assert session is not None
        assert session.errors == 1
        if logs:
            _, entries = _sink_texts(stream, transport)
            assert any(
                entry["body"] == TRACED_RECORD
                and entry["trace_id"] == transaction.trace_id
                for entry in entries
            )


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("holder", ["propagation", "span"])
@_safe_failure
def test_sdk_trace_objects_holding_application_values_are_read_natively(
    monkeypatch: pytest.MonkeyPatch, holder: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        text = wire.hooked("trace", str)
        items = cast(
            "dict[str, str]",
            wire.hooked("baggage", dict)({"trace_id": "1" * 32, "release": "r"}),
        )
        # Exact SDK trace objects whose own fields hold application objects, set
        # through their public constructors and attributes.
        if holder == "propagation":
            isolation = sentry_sdk.get_isolation_scope()
            saved = isolation._propagation_context
            isolation._propagation_context = PropagationContext(
                "1" * 32, "2" * 16, baggage=Baggage(items)
            )
            expected = "1" * 32
            try:
                _hostile_trace_captures(wire, logger)
            finally:
                isolation._propagation_context = saved
        else:
            with (
                sentry_sdk.start_transaction(name="fix8-trace-holder") as started,
                sentry_sdk.start_span(op="fix8.holder") as span,
            ):
                transaction = cast("Transaction", started)
                span.status = text("ok")
                span.description = text("ordinary")
                transaction.name = text("fix8-trace-holder")
                transaction._baggage = Baggage(items, mutable=False)
                try:
                    _hostile_trace_captures(wire, logger)
                finally:
                    span.status = "ok"
                    span.description = "ordinary"
                    transaction.name = "fix8-trace-holder"
                    transaction._baggage = None
            expected = transaction.trace_id
        _assert_clean(stream, client, transport, wire.calls)
        assert {OUTER + " message", OUTER + " exception"} <= _messages(transport)
        assert {
            event["contexts"]["trace"]["trace_id"]
            for event in transport.events
            if _messages_of(event) & {OUTER + " message", OUTER + " exception"}
        } == {expected}


def _hostile_trace_captures(wire: _Tripwire, logger: logging.Logger) -> None:
    wire.armed = True
    try:
        sentry_sdk.capture_message(OUTER + " message")
        sentry_sdk.capture_exception(RuntimeError(OUTER + " exception"))
        logger.error(RECORD)
    finally:
        wire.armed = False


@_safe_failure
def test_a_scope_fork_never_shares_feature_flags_with_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _sinks(monkeypatch) as (_, stream, client, transport):
        parent = sentry_sdk.get_isolation_scope()
        parent.flags.set("fix8-parent", result=True)
        with sentry_sdk.isolation_scope():
            add_feature_flag("fix8-child", result=True)
        assert [flag["flag"] for flag in parent.flags.get()] == ["fix8-parent"]
        _assert_clean(stream, client, transport, [])


def _messages_of(source: object) -> set[str]:
    event = cast("dict[str, Any]", source)
    found = set()
    for text in (
        event.get("message"),
        (event.get("logentry") or {}).get("message"),
        *(
            value.get("value")
            for value in (event.get("exception") or {}).get("values", [])
        ),
    ):
        if isinstance(text, str) and text.startswith(OUTER):
            found.add(text)
    return found


def _hooked_level(wire: _Tripwire) -> type[IntEnum]:
    """An IntEnum whose own value protocols record any call."""

    class Level(IntEnum):
        ERROR = logging.ERROR
        ONE = 1

        def __hash__(self) -> int:
            wire.trip("level.__hash__")
            return int.__hash__(self)

        def __eq__(self, other: object) -> bool:
            wire.trip("level.__eq__")
            return int.__index__(self) == other

        def __index__(self) -> int:
            wire.trip("level.__index__")
            return int.__index__(self)

        def __int__(self) -> int:
            wire.trip("level.__int__")
            return int.__index__(self)

        def __bool__(self) -> bool:
            wire.trip("level.__bool__")
            return int.__index__(self) != 0

        def __str__(self) -> str:
            wire.trip("level.__str__")
            return "ordinary level"

    return Level


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(
    "field", ["intenum-level", "bool-level", "int-stack-info", "intenum-stacklevel"]
)
@_safe_failure
def test_integer_valued_record_controls_are_normalised_not_withheld(
    monkeypatch: pytest.MonkeyPatch, field: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)

        level = _hooked_level(wire)
        options: dict[str, Any] = {
            "level": logging.ERROR,
            "msg": OUTER,
            "args": (),
            "extra": {"password": CANARY},
        }
        expected = "ERROR"
        if field == "intenum-level":
            options["level"] = level["ERROR"]
        elif field == "bool-level":
            options["level"] = True
            expected = "Level 1"
        elif field == "int-stack-info":
            options["stack_info"] = 1
        else:
            options["stacklevel"] = level["ONE"]
        wire.armed = True
        logger._log(**options)
        wire.armed = False
        _assert_clean(stream, client, transport, wire.calls)
        console = stream.getvalue()
        assert OUTER in console
        assert WITHHELD not in console
        assert expected in console
        if field == "int-stack-info":
            assert "Stack (most recent call last)" in console

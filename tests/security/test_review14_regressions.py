"""Span and transaction finish never run application code, explicit or ambient."""

from __future__ import annotations

import warnings
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
import sentry_sdk
from sentry_sdk.ai.utils import set_conversation_id
from sentry_sdk.consts import DEFAULT_MAX_BREADCRUMBS
from sentry_sdk.envelope import Envelope
from sentry_sdk.tracing import Span, Transaction, _SpanRecorder
from sentry_sdk.tracing_utils import Baggage

from config.settings import prod
from tests.security.test_inert_diagnostics import _safe_failure
from tests.security.test_redaction_boundary_regressions import _sinks
from tests.security.test_review13_regressions import (
    CANARY,
    OUTER,
    PROCESSORS,
    _assert_clean,
    _cases,
    _target,
    _Tripwire,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.security.test_redaction_boundary_regressions import _EveryEnvelope

TRANSACTION = "fix9 transaction"
CHILD = "fix9 child"
# The type each Span or Transaction slot holds when the SDK fills it, keyed by the
# SDK's own slot lists rather than by the boundary's projection lists.
SPAN_SLOT_TYPES: dict[str, type | None] = {
    "_trace_id": str,
    "_span_id": str,
    "parent_span_id": str,
    "same_process_as_parent": None,
    "sampled": None,
    "op": str,
    "description": str,
    "_measurements": dict,
    "start_timestamp": datetime,
    "_start_timestamp_monotonic_ns": int,
    "status": str,
    "timestamp": datetime,
    "_tags": dict,
    "_data": dict,
    "_span_recorder": _SpanRecorder,
    "hub": None,
    "_context_manager_state": None,
    "_containing_transaction": None,
    "scope": sentry_sdk.Scope,
    "origin": str,
    "name": str,
    "_flags": dict,
    "_flags_capacity": int,
    "source": str,
    "parent_sampled": None,
    "sample_rate": float,
    "_contexts": dict,
    "_profile": None,
    "_continuous_profile": None,
    "_baggage": Baggage,
    "_sample_rand": float,
}
SUBCLASS_ARGUMENTS: dict[type, tuple[object, ...]] = {
    str: ("ordinary",),
    int: (1,),
    float: (0.5,),
    datetime: (2026, 10, 30, 12, 0, 0, 0, UTC),
    _SpanRecorder: (1000,),
    Baggage: ({"trace_id": "1" * 32},),
}
ROWS = (
    "ambient-conversation-span",
    "ambient-conversation-transaction",
    "explicit-conversation-span",
    "explicit-conversation-transaction",
    "status",
    "op",
    "name",
    "control",
)


def _member(kind: type, name: str) -> Any:  # noqa: ANN401 - an SDK slot descriptor
    return next(base.__dict__[name] for base in kind.__mro__ if name in base.__dict__)


def _span_cases() -> list[tuple[str, str, str]]:
    cases: list[tuple[str, str, str]] = []
    for owner in ("span", "finished-span", "transaction"):
        slots = (
            (*Span.__slots__, *Transaction.__slots__)
            if owner == "transaction"
            else Span.__slots__
        )
        for slot in dict.fromkeys(slots):
            kinds = ["opaque"]
            if SPAN_SLOT_TYPES[slot] is not None:
                kinds.append("subclass")
            if SPAN_SLOT_TYPES[slot] in {dict, Baggage}:
                kinds.append("item")
            cases.extend((owner, slot, kind) for kind in kinds)
    return cases


def _span_value(wire: _Tripwire, slot: str, kind: str) -> object:
    if kind == "opaque":
        return wire.hooked(slot, object)()
    base = cast("type", SPAN_SLOT_TYPES[slot])
    if kind == "item":
        mapping = {"password": CANARY, "hostile": wire.hooked(slot, object)()}
        return Baggage(mapping) if base is Baggage else mapping
    if base is dict:
        return wire.hooked(slot, dict)({"password": CANARY, "ordinary": "fix9"})
    return wire.hooked(slot, base)(*SUBCLASS_ARGUMENTS.get(base, ()))


def _started() -> tuple[Transaction, Span]:
    transaction = cast(
        "Transaction",
        sentry_sdk.start_transaction(name=TRANSACTION, op="fix9.transaction"),
    )
    # A redis span: its finish also builds a breadcrumb for the scope it ends on.
    return transaction, transaction.start_child(op="db.redis", name=CHILD)


def _raised(*calls: Callable[[], object]) -> list[str]:
    """Run each telemetry call; none may raise, so keep the type of any that did."""
    raised = []
    for call in calls:
        try:
            call()
        except Exception as error:  # noqa: BLE001 - a raise is itself the finding
            raised.append(type.__dict__["__name__"].__get__(type(error)))
    return raised


def _transactions(transport: _EveryEnvelope) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", event)
        for event in transport.events
        if event.get("type") == "transaction"
    ]


def _probe(row: str, value: str) -> None:
    """Review 14's span-probe rows, each value set through a public SDK API."""
    if row.startswith("ambient-conversation"):
        set_conversation_id(value)
        if row.endswith("span"):
            with sentry_sdk.start_span(op="fix9.child", name=CHILD):
                return
        with (
            sentry_sdk.start_transaction(name=TRANSACTION),
            sentry_sdk.start_span(op="fix9.child", name=CHILD),
        ):
            return
    if row.startswith("explicit-conversation"):
        explicit = sentry_sdk.Scope()
        explicit.set_conversation_id(value)
        transaction, child = _started()
        child.finish(scope=explicit if row.endswith("span") else None)
        transaction.finish(scope=explicit if row.endswith("transaction") else None)
        return
    with sentry_sdk.start_transaction(name=TRANSACTION) as started:
        op = value if row == "op" else "fix9.child"
        with sentry_sdk.start_span(op=op, name=CHILD) as span:
            if row == "status":
                span.set_status(value)
            if row == "name":
                # Set after the transaction started; still in place when a child
                # ends and when the transaction itself finishes.
                cast("Transaction", started).name = value


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("row", ROWS)
@_safe_failure
def test_review14_span_probe_rows_run_no_application_code(
    monkeypatch: pytest.MonkeyPatch, row: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        text = "ordinary" if row == "control" else wire.hooked(row, str)("ordinary")
        wire.armed = True
        try:
            raised = _raised(lambda: _probe(row, cast("str", text)))
        finally:
            wire.armed = False
            sentry_sdk.get_current_scope().remove_conversation_id()
        _assert_clean(stream, client, transport, wire.calls)
        assert raised == []
        expected = {"ambient-conversation-span": [], "name": ["ordinary"]}
        delivered = _transactions(transport)
        assert [event["transaction"] for event in delivered] == expected.get(
            row, [TRANSACTION]
        )
        if row in {"status", "op"}:
            # The projection of the application's text is what the trace carries.
            [span] = delivered[0]["spans"]
            assert span[row] == "ordinary"


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(("owner", "slot", "kind"), _span_cases())
@_safe_failure
def test_every_span_slot_is_inert_at_finish(
    monkeypatch: pytest.MonkeyPatch, owner: str, slot: str, kind: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        transaction, child = _started()
        target: Span = transaction if owner == "transaction" else child
        # A child's own finish reads its slots and, once it has finished, its
        # transaction's finish serializes them; a transaction reads its own.
        finishes: list[Callable[[], object]] = [child.finish, transaction.finish]
        if owner != "span":
            child.finish()
            finishes = [transaction.finish]
        _member(type(target), slot).__set__(target, _span_value(wire, slot, kind))
        wire.armed = True
        try:
            raised = _raised(*finishes)
        finally:
            wire.armed = False
        _assert_clean(stream, client, transport, wire.calls)
        assert raised == []
        if owner == "span" or (
            kind != "opaque" and SPAN_SLOT_TYPES[slot] in {str, dict}
        ):
            # A child that cannot be owned is dropped, never its transaction; the
            # text and mappings a span carries are projected, not refused.
            assert len(_transactions(transport)) == 1


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("kind", ["opaque", "subclass"])
@_safe_failure
def test_a_transaction_recording_a_foreign_span_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, kind: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        transaction, child = _started()
        child.finish()
        recorder = transaction._span_recorder
        assert recorder is not None
        foreign = wire.hooked("recorded", object if kind == "opaque" else Span)()
        cast("list[object]", recorder.spans).append(foreign)
        wire.armed = True
        try:
            raised = _raised(transaction.finish)
        finally:
            wire.armed = False
        _assert_clean(stream, client, transport, wire.calls)
        assert raised == []
        assert _transactions(transport) == []


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(("source", "slot", "kind"), _cases())
@_safe_failure
def test_every_scope_slot_is_inert_at_finish(
    monkeypatch: pytest.MonkeyPatch, source: str, slot: str, kind: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        value = wire.value(slot, kind)
        target = _target(source)
        transaction, child = _started()
        storage = sentry_sdk.Scope.__dict__[slot]
        saved = storage.__get__(target)
        storage.__set__(target, value)
        explicit: dict[str, Any] = {"scope": target} if source == "explicit" else {}
        wire.armed = True
        try:
            raised = _raised(
                lambda: child.finish(**explicit),
                lambda: transaction.finish(**explicit),
            )
        finally:
            wire.armed = False
            storage.__set__(target, saved)
        _assert_clean(stream, client, transport, wire.calls)
        assert raised == []
        if kind != "opaque" and slot not in PROCESSORS:
            # Native storage of a replaced container is still read, not refused.
            assert [event["transaction"] for event in _transactions(transport)] == [
                TRANSACTION
            ]


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_finished_traces_keep_their_native_diagnostics(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (_, stream, client, transport):
        set_conversation_id("fix9-conversation")
        transaction = cast(
            "Transaction",
            sentry_sdk.start_transaction(name=TRANSACTION, op="fix9.transaction"),
        )
        with transaction:
            transaction.set_tag("fix9.tag", "transaction")
            transaction.set_context("fix9", {"stage": "finish"})
            transaction.set_data("password", CANARY)
            with sentry_sdk.start_span(op="fix9.child", name=CHILD) as span:
                span.set_tag("fix9.tag", "child")
                span.set_data("password", CANARY)
                span.set_status("ok")
            with sentry_sdk.start_span(op="gen_ai.chat", name="fix9 chat") as chat:
                pass
            transaction.set_status("internal_error")
        sentry_sdk.get_current_scope().remove_conversation_id()
        _assert_clean(stream, client, transport, [])
        # The SDK still adds the ambient conversation id, as an exact string, and
        # streams that gen_ai span as its own item after the transaction's scrub.
        assert chat._data["gen_ai.conversation.id"] == "fix9-conversation"
        assert "span" in transport.item_types
        [event] = _transactions(transport)
        trace = event["contexts"]["trace"]
        assert event["transaction"] == TRANSACTION
        assert event["transaction_info"] == {"source": "custom"}
        assert event["tags"]["fix9.tag"] == "transaction"
        assert event["contexts"]["fix9"] == {"stage": "finish"}
        assert (trace["op"], trace["status"]) == ("fix9.transaction", "internal_error")
        assert (trace["trace_id"], trace["span_id"]) == (
            transaction.trace_id,
            transaction.span_id,
        )
        # Span data travels under the provider-body name "data", withheld whole.
        assert trace["data"] == prod.SENTRY_REDACTION_MARKER
        [child] = event["spans"]
        assert (child["op"], child["description"], child["status"]) == (
            "fix9.child",
            CHILD,
            "ok",
        )
        assert child["parent_span_id"] == transaction.span_id
        assert child["tags"]["fix9.tag"] == "child"
        assert child["data"] == prod.SENTRY_REDACTION_MARKER
        header = next(
            Envelope.deserialize(data).headers
            for data in transport.payloads
            if b'"type":"transaction"' in data
        )
        assert header["trace"]["trace_id"] == transaction.trace_id
        assert header["trace"]["transaction"] == TRANSACTION


@pytest.mark.parametrize("logs", [False, True])
@_safe_failure
def test_span_breadcrumbs_stay_on_the_ambient_scope_only(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (_, stream, client, transport):
        explicit = sentry_sdk.Scope()
        with sentry_sdk.start_span(op="http.client", name="GET fix9") as span:
            span.set_data("url", "https://example.invalid/fix9")
            span.set_data("http.response.status_code", 503)
        with sentry_sdk.start_span(op="db.redis", name="GET fix9-key"):
            pass
        sentry_sdk.start_span(op="subprocess", name="fix9 process").finish(
            scope=explicit
        )
        sentry_sdk.capture_message(OUTER)
        _assert_clean(stream, client, transport, [])
        event = next(
            cast("dict[str, Any]", event)
            for event in transport.events
            if event.get("message") == OUTER
        )
        crumbs = event["breadcrumbs"]["values"]
        assert [
            (crumb["type"], crumb["category"], crumb.get("level")) for crumb in crumbs
        ] == [("http", "httplib", "error"), ("redis", "redis", None)]
        assert crumbs[1]["message"] == "GET fix9-key"
        # A caller's finish scope is read as projections, never written to.
        assert explicit._breadcrumbs == deque()
        assert explicit._n_breadcrumbs_truncated == 0


@_safe_failure
def test_span_breadcrumbs_keep_the_sdk_retention_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _sinks(monkeypatch) as (_, stream, client, transport):
        current = sentry_sdk.get_current_scope()
        for index in range(DEFAULT_MAX_BREADCRUMBS):
            current.add_breadcrumb({"message": f"fix9 crumb {index}"})
        truncated = current._n_breadcrumbs_truncated
        with sentry_sdk.start_span(op="db.redis", name="GET fix9-key"):
            pass
        messages = [crumb["message"] for crumb in current._breadcrumbs]
        assert len(messages) == DEFAULT_MAX_BREADCRUMBS
        assert (messages[0], messages[-1]) == ("fix9 crumb 1", "GET fix9-key")
        assert current._n_breadcrumbs_truncated == truncated + 1
        _assert_clean(stream, client, transport, [])


@_safe_failure
def test_a_finish_scope_is_read_as_projections_but_never_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _sinks(monkeypatch) as (_, stream, client, transport):
        explicit = sentry_sdk.Scope()
        explicit.set_tag("fix9.layer", "explicit")
        explicit.set_extra("password", CANARY)
        explicit.set_conversation_id("fix9-conversation")
        before = {
            name: sentry_sdk.Scope.__dict__[name].__get__(explicit)
            for name in sentry_sdk.Scope.__slots__
        }
        copies = {
            name: (dict(value) if type(value) is dict else list(value))
            for name, value in before.items()
            if type(value) in {dict, list, deque}
        }
        transaction, _ = _started()
        child = transaction.start_child(op="gen_ai.chat", name=CHILD)
        child.finish(scope=explicit)
        transaction.finish(scope=explicit)
        for name, value in before.items():
            assert sentry_sdk.Scope.__dict__[name].__get__(explicit) is value, name
        for name, copied in copies.items():
            current = before[name]
            assert (dict(current) if type(current) is dict else list(current)) == (
                copied
            ), name
        _assert_clean(stream, client, transport, [])
        [event] = _transactions(transport)
        assert event["tags"]["fix9.layer"] == "explicit"
        assert event["extra"]["password"] == prod.SENTRY_REDACTION_MARKER
        assert "span" in transport.item_types
        # The owned copy carries the caller's conversation id as an exact string.
        assert child._data["gen_ai.conversation.id"] == "fix9-conversation"


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(
    "argument", ["hub", "scope-subclass", "scope-opaque", "end-timestamp"]
)
@_safe_failure
def test_a_finish_that_cannot_be_owned_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, argument: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        transaction, child = _started()
        child.finish()
        options: dict[str, Any] = {}
        if argument == "hub":
            options["hub"] = wire.hooked("hub", object)()
        elif argument == "scope-subclass":
            options["scope"] = wire.hooked("scope", sentry_sdk.Scope)()
        elif argument == "scope-opaque":
            options["scope"] = wire.hooked("scope", object)()
        else:
            options["end_timestamp"] = wire.hooked("end_timestamp", float)(1.0)
        wire.armed = True
        try:
            with warnings.catch_warnings():
                # The SDK deprecates hub=; only the dispatch is under test here.
                warnings.simplefilter("ignore", DeprecationWarning)
                raised = _raised(lambda: transaction.finish(**options))
        finally:
            wire.armed = False
        _assert_clean(stream, client, transport, wire.calls)
        assert raised == []
        assert _transactions(transport) == []


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize("api", ["count", "gauge", "distribution"])
@_safe_failure
def test_sdk_metrics_are_off_and_never_read_their_arguments(
    monkeypatch: pytest.MonkeyPatch, api: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        wire = _Tripwire(logger)
        name = wire.hooked("metric.name", str)("fix9.metric")
        attributes = wire.hooked("metric.attributes", dict)(
            {"password": CANARY, "ordinary": "fix9"}
        )
        wire.armed = True
        try:
            raised = _raised(
                lambda: getattr(sentry_sdk.metrics, api)(
                    name, 1.0, attributes=attributes
                )
            )
        finally:
            wire.armed = False
        _assert_clean(stream, client, transport, wire.calls)
        assert raised == []
        assert prod.SENTRY_OPTIONS["enable_metrics"] is False
        assert client.metrics_batcher is None
        assert "trace_metric" not in transport.item_types

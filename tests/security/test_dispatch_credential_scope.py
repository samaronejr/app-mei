"""A record's own credentials bind its whole dispatch; event hooks own plain dicts."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
import sentry_sdk

from apps.delivery.privacy import delivery_secrets
from config.settings import prod
from tests.security.test_redaction_boundary_regressions import (
    CANARY,
    LOGGER,
    _EveryEnvelope,
    _RejectClear,
    _RejectMarker,
    _sinks,
)
from tests.security.test_withhold_boundaries import _MissingStack

if TYPE_CHECKING:
    from _collections_abc import dict_items, dict_keys

RECORD_LOCAL = "zqrecordlocal7351973519qz"
REVIVED = "revived carrier diagnostic"
INJECTED = "injected by the carrier update"
MONITOR = "fix4-session3-monitor"


class _AttributeRenderedError(Exception):
    def __init__(self, value: str) -> None:
        super().__init__("attribute-rendered failure")
        self.value = value

    def __str__(self) -> str:
        return f"attribute-rendered failure {self.value}"


class _Opaque:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"opaque({self.value})"


def _failure(kind: str) -> Exception:
    if kind == "note":
        error = RuntimeError("note-bearing failure")
        error.add_note(f"context {RECORD_LOCAL}")
        return error
    if kind == "attribute":
        return _AttributeRenderedError(RECORD_LOCAL)
    return RuntimeError("ordinary failure")


def _raise(error: Exception) -> NoReturn:
    raise error


@pytest.mark.parametrize("kind", ["note", "attribute", "opaque-extra"])
def test_a_record_local_credential_is_hidden_at_every_sink_of_its_dispatch(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Given a credential known only from the record's own password field.
    extra: dict[str, object] = {"password": RECORD_LOCAL}
    if kind == "opaque-extra":
        extra["detail"] = _Opaque(RECORD_LOCAL)
    failure = _failure(kind)
    with _sinks(monkeypatch) as (logger, stream, client, transport):
        # When its live exception is logged through the console and the full SDK.
        try:
            _raise(failure)
        except Exception:
            logger.exception("record-local failure", extra=extra)
        sentry_sdk.capture_message("later breadcrumb-bearing event")
        client.flush()
    # Then no pass or envelope renders it, and the dispatch left nothing behind.
    delivered = b"".join(transport.payloads).decode()
    assert RECORD_LOCAL not in stream.getvalue() + delivered
    assert prod._known_values() == set()
    # And the diagnostic, its exception type and the later breadcrumb still flow.
    events: list[Any] = json.loads(json.dumps(transport.events))
    assert "record-local failure" in stream.getvalue()
    logged = next(event for event in events if event.get("exception"))
    assert logged["exception"]["values"][-1]["type"] == type(failure).__name__
    later = next(
        event
        for event in events
        if event.get("message") == "later breadcrumb-bearing event"
    )
    assert "record-local failure" in json.dumps(later.get("breadcrumbs"))


class _EchoingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.name == LOGGER and record.exc_info:
            logging.getLogger(f"{LOGGER}.nested").error(
                "nested echo %s", record.exc_info[1]
            )


def test_a_record_logged_during_a_dispatch_inherits_its_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a handler that renders the live exception again during the dispatch.
    echo = _EchoingHandler()
    with _sinks(monkeypatch) as (logger, stream, client, transport):
        logger.addHandler(echo)
        try:
            # When the outer credential is known only from the outer record's field.
            try:
                _raise(_AttributeRenderedError(RECORD_LOCAL))
            except _AttributeRenderedError:
                logger.exception("outer failure", extra={"password": RECORD_LOCAL})
        finally:
            logger.removeHandler(echo)
        client.flush()
    # Then the nested record is scrubbed with the outer record's values too.
    delivered = b"".join(transport.payloads).decode()
    assert "nested echo" in stream.getvalue()
    assert RECORD_LOCAL not in stream.getvalue() + delivered


class _ForeignMapping(MutableMapping[str, object]):
    def __init__(self, data: dict[str, object]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


@pytest.mark.parametrize("scrub", ["ordinary", "unavailable-stack"])
def test_a_foreign_event_mapping_is_refused_without_raising(
    monkeypatch: pytest.MonkeyPatch, scrub: str
) -> None:
    # Given a complete event mapping that is not a dict.
    event: Any = _ForeignMapping(
        {"message": "foreign mapping diagnostic", "level": "error"}
    )
    with _sinks(monkeypatch) as (_, _, client, transport):
        with monkeypatch.context() as broken:
            if scrub == "unavailable-stack":
                broken.setattr(prod, "_task_stack", _MissingStack())
            # When the application captures it through the real client.
            event_id = sentry_sdk.capture_event(event)
        sentry_sdk.capture_message("ordinary control event")
        client.flush()
    # Then nothing raised, the carrier was refused and ordinary events still flow.
    delivered = b"".join(transport.payloads).decode()
    assert event_id is None
    assert "foreign mapping diagnostic" not in delivered
    assert "ordinary control event" in delivered


def test_a_plain_client_still_receives_a_foreign_event_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the production boundary installed in this interpreter, and a plain client.
    with _sinks(monkeypatch):
        pass
    transport = _EveryEnvelope()
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1", transport=transport
    )
    event: Any = _ForeignMapping({"message": "plain client mapping", "level": "error"})
    try:
        with sentry_sdk.isolation_scope() as scope, sentry_sdk.new_scope():
            scope.clear()
            scope.set_client(client)
            # When that client captures a mapping that is not a dict.
            sentry_sdk.capture_event(event)
            client.flush()
    finally:
        client.close()
    # Then the production refusal is not imposed on it.
    assert "plain client mapping" in b"".join(transport.payloads).decode()


class _RevivingCarrier(dict[str, object]):
    reads = 0

    def _offered(self) -> dict[str, object]:
        type(self).reads += 1
        if type(self).reads == 1:
            message = "first carrier read failed"
            raise RuntimeError(message)
        return {"message": REVIVED, "level": "error"}

    def items(self) -> dict_items[str, object]:
        return self._offered().items()

    def keys(self) -> dict_keys[str, object]:
        return self._offered().keys()

    def __iter__(self) -> Iterator[str]:
        return iter(self._offered())


def test_a_dict_subclass_carrier_cannot_revive_or_steer_its_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a carrier whose accessors would fail the scrub, then offer another event.
    monkeypatch.setattr(_RevivingCarrier, "reads", 0)
    event: Any = _RevivingCarrier(message="ordinary carrier diagnostic", level="error")
    with _sinks(monkeypatch) as (_, _, client, transport):
        # When the application captures it through the real client.
        sentry_sdk.capture_event(event)
        client.flush()
    # Then the hooks owned a plain copy of its storage and never called it.
    delivered = b"".join(transport.payloads).decode()
    assert _RevivingCarrier.reads == 0
    assert "ordinary carrier diagnostic" in delivered
    assert REVIVED not in delivered
    # And the caller's carrier keeps only the withheld marker.
    assert dict(dict.items(event)) == {prod._WITHHELD_EVENT: True}


def _check_in(kind: type[dict[str, object]], **fields: object) -> dict[str, object]:
    return kind(
        type="check_in",
        monitor_slug=MONITOR,
        status="ok",
        check_in_id="0" * 32,
        **fields,
    )


@pytest.mark.parametrize("kind", [_RejectClear, _RejectMarker])
def test_a_check_in_scrub_failure_cannot_escape_through_carrier_mutators(
    monkeypatch: pytest.MonkeyPatch, kind: type[dict[str, object]]
) -> None:
    # Given a check-in, which the SDK hands to the scrubber hook without processors.
    event: Any = _check_in(kind, message=CANARY)
    with _sinks(monkeypatch) as (_, _, client, transport):
        with monkeypatch.context() as broken:
            broken.setattr(prod, "_task_stack", _MissingStack())
            # When its scrub fails inside the real client.
            sentry_sdk.capture_event(event)
        sentry_sdk.capture_message("ordinary control event")
        client.flush()
    # Then the husk used dict primitives: nothing escaped, nothing original remains.
    delivered = b"".join(transport.payloads).decode()
    assert CANARY not in repr(event) + delivered
    assert "ordinary control event" in delivered


class _InjectingUpdate(dict[str, object]):
    def update(self, *_args: object, **_kwargs: object) -> None:
        dict.__setitem__(self, "injected", INJECTED)


def test_a_check_in_scrub_commits_without_the_carriers_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a check-in carrier whose own update injects a field.
    event: Any = _check_in(_InjectingUpdate)
    with _sinks(monkeypatch) as (_, _, client, transport):
        # When the scrubber hook replaces its contents after a successful scrub.
        sentry_sdk.capture_event(event)
        client.flush()
    # Then only the scrubbed snapshot was committed, and the check-in was sent.
    delivered = b"".join(transport.payloads).decode()
    assert INJECTED not in delivered
    assert MONITOR in delivered


def test_a_foreign_check_in_mapping_never_reaches_a_dict_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a non-dict check-in, which skips the processor that refuses it elsewhere.
    event: Any = _ForeignMapping(
        {
            "type": "check_in",
            "monitor_slug": MONITOR,
            "status": "ok",
            "check_in_id": "0" * 32,
            "message": CANARY,
        }
    )
    with _sinks(monkeypatch) as (_, _, client, transport):
        with delivery_secrets(CANARY):
            # When it is captured through the real client.
            sentry_sdk.capture_event(event)
        client.flush()
    # Then the hook left it untouched rather than raising, and before_send still
    # scrubbed the SDK's serialized plain copy.
    assert CANARY not in b"".join(transport.payloads).decode()

"""Boundary interactions that distinguish each fail-closed guard from its mutant."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest
import sentry_sdk
from celery import _state

from apps.delivery.privacy import delivery_secrets
from config.settings import prod
from tests.security.test_redaction_boundary_regressions import CANARY, _sinks
from tests.security.test_withhold_boundaries import _MissingStack, _RejectUpdate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sentry_sdk.types import Event, Hint


def test_a_dropped_standard_record_hint_cannot_revive_its_original_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a standard record already rejected after a secondary failure.
    record = logging.makeLogRecord({"msg": CANARY, "exc_info": [None]})

    def failing_withhold(*_args: object) -> None:
        message = "secondary failure"
        raise RuntimeError(message)

    with monkeypatch.context() as broken:
        broken.setattr(prod, "_withhold", failing_withhold)
        assert prod.CredentialURLLogFilter().filter(record) is False
    with _sinks(monkeypatch) as (_, _, client, transport):
        # When direct SDK calls carry that original as a hint.
        sentry_sdk.capture_event({"message": CANARY}, hint={"log_record": record})
        sentry_sdk.add_breadcrumb(message=CANARY, hint={"log_record": record})
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then neither the event nor the retained breadcrumb can revive it.
    assert len(transport.events) == 1
    assert CANARY not in b"".join(transport.payloads).decode()


def test_the_private_stack_name_is_resolved_again_at_each_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a stack name that disappears after settings have loaded.
    with _sinks(monkeypatch) as (logger, stream, client, transport):
        with monkeypatch.context() as broken:
            broken.delattr(_state, "_task_stack")
            # When console and SDK boundaries need the now-unavailable context.
            logger.error(CANARY)
            sentry_sdk.capture_message(CANARY)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then they fail closed, while delivery resumes after the dependency recovers.
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert prod.SENTRY_REDACTION_MARKER in stream.getvalue()
    assert len(transport.events) == 1


def test_an_already_withheld_event_cannot_lose_its_drop_marker_on_a_second_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an event the raw SDK hook has already withheld.
    event: Any = {"message": CANARY}
    with _sinks(monkeypatch) as (_, _, client, transport):
        with monkeypatch.context() as broken:
            broken.setattr(prod, "_task_stack", _MissingStack())
            sentry_sdk.capture_event(event)
        # When another capture could redact even the marker's own schema key.
        with delivery_secrets(prod._WITHHELD_EVENT):
            sentry_sdk.capture_event(event)
        client.flush()
    # Then withholding is irrevocable rather than dependent on the next scrub.
    assert transport.events == []
    assert CANARY not in repr(event)


def test_a_successful_scrub_replaces_fields_injected_during_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a source that changes after its scrubbed snapshot has been built.
    payload: dict[str, object] = {
        "message": "ordinary diagnostic",
        "extra": {"body": CANARY},
    }
    event: Any = payload
    original = prod.scrub_sentry_event

    def inject(source: Mapping[str, object], hint: Hint) -> Event:
        scrubbed = original(source, hint)
        if source is event:
            payload["late_original"] = CANARY
        return scrubbed

    with _sinks(monkeypatch) as (_, _, client, transport):
        monkeypatch.setattr(prod, "scrub_sentry_event", inject)
        # When the SDK's in-place hook commits the successful scrub.
        sentry_sdk.capture_event(event)
        client.flush()
    # Then only the safe snapshot survives, both in memory and at the transport.
    assert len(transport.events) == 1
    assert CANARY not in repr(event) + b"".join(transport.payloads).decode()


class _InjectingUpdate(dict[str, object]):
    def update(self, *args: object, **kwargs: object) -> None:
        # SDK updates pass mappings; retain that behavior before injecting.
        for given in args:
            dict.update(self, cast("Mapping[str, object]", given))
        dict.update(self, kwargs)
        self["late_original"] = CANARY


def test_committing_a_scrub_cannot_call_a_hostile_event_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an event whose update appends an original credential.
    event: Any = _InjectingUpdate(message="ordinary diagnostic", extra={"body": CANARY})
    with _sinks(monkeypatch) as (_, _, client, transport):
        # When the real SDK captures it with the production scrubber.
        sentry_sdk.capture_event(event)
        client.flush()
    # Then no overridable update can append data after the safe snapshot.
    assert len(transport.events) == 1
    assert CANARY not in repr(event) + json.dumps(transport.events)


def test_excessive_diagnostics_are_withheld_at_real_console_and_sdk_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a body beyond the resource envelope with an unlabelled credential echo.
    body = CANARY + "? " * (300 * 1024)
    with _sinks(monkeypatch) as (logger, stream, client, transport):
        # When logging and SDK capture encounter the excessive diagnostic.
        logger.warning("body echo %s", CANARY, extra={"payload": body})
        sentry_sdk.capture_event({"message": CANARY, "extra": {"body": body}})
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then no prefix is delivered and later ordinary telemetry still flows.
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert prod.SENTRY_REDACTION_MARKER in stream.getvalue()
    assert len(transport.events) == 2
    assert transport.events[0].get("logger") == prod.SENTRY_REDACTION_MARKER
    assert transport.events[1].get("message") == "ordinary following event"


_CANARY_ERROR: type[RuntimeError] = type(CANARY, (RuntimeError,), {})


class _PrivateErrorName(dict[str, Any]):
    def update(self, *_args: object, **_kwargs: object) -> None:
        raise _CANARY_ERROR


@pytest.mark.parametrize("source", ["error-name", "numeric-level"])
def test_withholding_cannot_reintroduce_private_error_names_or_numeric_levels(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    # Given an original credential also encoded in fallback diagnostic metadata.
    canary = CANARY if source == "error-name" else "9031790317"
    record = logging.makeLogRecord(
        {
            "name": "axrm011.fix4.boundary",
            "msg": canary,
            "levelno": logging.ERROR if source == "error-name" else int(canary),
        }
    )
    dictionary = _PrivateErrorName if source == "error-name" else _RejectUpdate
    record.__dict__ = dictionary(record.__dict__)
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        # When the entire record must be withheld through every real sink.
        logger.handle(record)
        sentry_sdk.capture_message("ordinary following event")
        client.flush()
    # Then reconstruction cannot turn an original credential into metadata.
    assert prod.SENTRY_REDACTION_MARKER in stream.getvalue()
    assert canary not in stream.getvalue() + b"".join(transport.payloads).decode()

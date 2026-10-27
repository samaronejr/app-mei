"""A failing withhold, or a missing Celery stack, still fails closed at every sink."""

import json
import logging
from typing import Any

import pytest
import sentry_sdk
from celery import _state

from config.settings import prod
from tests.security.test_credential_log_hygiene import _CaptureTransport
from tests.security.test_task_failure_log_privacy import MARKER, _production_console

CANARY = "synthetic-boundary-secret-canary-9001"
LOGGER_NAME = "axrm011.withhold.boundary"


class _RejectGet(dict[str, Any]):
    def get(self, *_args: object, **_kwargs: object) -> object:
        message = "secondary get failure"
        raise RuntimeError(message)


class _RejectClear(dict[str, Any]):
    def clear(self) -> None:
        message = "secondary clear failure"
        raise RuntimeError(message)


class _RejectUpdate(dict[str, Any]):
    def update(self, *_args: object, **_kwargs: object) -> None:
        message = "secondary update failure"
        raise RuntimeError(message)


class _MissingStack:
    @property
    def stack(self) -> object:
        message = "renamed Celery stack"
        raise AttributeError(message)


def _raise_secret() -> None:
    message = CANARY
    raise RuntimeError(message)


def _sentry() -> tuple[sentry_sdk.Client, _CaptureTransport]:
    transport = _CaptureTransport()
    client = sentry_sdk.Client(
        **{
            **prod.SENTRY_OPTIONS,
            "dsn": "http://public@example.invalid/1",
            "transport": transport,
        }
    )
    return client, transport


@pytest.mark.parametrize(
    "hostile",
    [_RejectGet, _RejectClear, _RejectUpdate],
    ids=["get", "clear", "update"],
)
def test_a_withhold_that_itself_fails_emits_nothing_original(
    monkeypatch: pytest.MonkeyPatch, hostile: type[dict[str, Any]]
) -> None:
    # Given a record whose mapping fails during the fallback, after a primary failure.
    handler, stream = _production_console()
    client, transport = _sentry()
    logger = logging.getLogger(LOGGER_NAME)
    monkeypatch.setattr(logger, "propagate", False)
    record = logging.makeLogRecord(
        {
            "name": LOGGER_NAME,
            "levelno": logging.ERROR,
            "levelname": "ERROR",
            "msg": CANARY,
            "private_extra": CANARY,
            "exc_info": [None],
        }
    )
    record.__dict__ = hostile(record.__dict__)
    logger.addHandler(handler)
    # When it passes the real production console and then the Sentry log integration.
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            logger.handle(record)
            sentry_sdk.capture_message("a following observation")
            client.flush()
    finally:
        logger.removeHandler(handler)
        client.close()
    # Then nothing raised, nothing original was emitted or delivered, all withheld.
    emitted = stream.getvalue()
    delivered = json.dumps(transport.events)
    assert CANARY not in emitted + delivered + repr(record.__dict__)
    assert MARKER in emitted
    assert MARKER in delivered


def test_a_record_whose_withhold_cannot_complete_is_dropped_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given any failure inside the withholding path itself.
    handler, stream = _production_console()
    record = logging.makeLogRecord({"msg": CANARY, "exc_info": [None]})

    def failing_withhold(*_args: object) -> None:
        message = "withhold failed"
        raise RuntimeError(message)

    monkeypatch.setattr(prod, "_withhold", failing_withhold)
    # When the public filter handles it.
    accepted = prod.CredentialURLLogFilter().filter(record)
    handler.handle(record)
    # Then it is rejected rather than raised, and the handler emits nothing.
    assert accepted is False
    assert stream.getvalue() == ""


@pytest.mark.parametrize("breakage", ["renamed-attribute", "deleted-name"])
def test_an_unavailable_task_stack_fails_closed_at_every_sink(
    monkeypatch: pytest.MonkeyPatch, breakage: str
) -> None:
    # Given a Celery upgrade that renamed what the redactor reads.
    if breakage == "renamed-attribute":
        monkeypatch.setattr(prod, "_task_stack", _MissingStack())
    else:
        monkeypatch.delattr(prod, "_task_stack")
    handler, stream = _production_console()
    client, transport = _sentry()
    logger = logging.getLogger(LOGGER_NAME)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    # When the console, a captured message and a captured exception all run.
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            logger.handle(
                logging.makeLogRecord(
                    {"name": LOGGER_NAME, "levelno": logging.ERROR, "msg": CANARY}
                )
            )
            sentry_sdk.capture_message(CANARY)
            try:
                _raise_secret()
            except RuntimeError:
                sentry_sdk.capture_exception()
            client.flush()
    finally:
        logger.removeHandler(handler)
        client.close()
    # Then nothing raised, the console withheld the record, Sentry delivered nothing.
    emitted = stream.getvalue()
    assert MARKER in emitted
    assert CANARY not in emitted
    assert transport.events == []


def test_the_private_celery_stack_keeps_the_shape_the_redactor_reads() -> None:
    stack = _state._task_stack.stack
    assert stack is None or isinstance(stack, list)

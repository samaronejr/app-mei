"""Inert diagnostics retain the operational fields approved in design (b)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from types import FunctionType
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID

import pytest
import sentry_sdk
from celery import Celery
from sentry_sdk.integrations import celery as sentry_celery

from apps.integrations.privacy import project_structure
from apps.integrations.transport_types import TransportFailure
from tests.security.test_redaction_boundary_regressions import _sinks

if TYPE_CHECKING:
    import logging

CANARY = "synthetic-fix6-projection-credential-693724"


def _raise(error: Exception) -> NoReturn:
    raise error


@pytest.mark.parametrize("logs", [False, True])
def test_types_codes_notes_chains_groups_and_code_frames_remain_useful(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    error = ExceptionGroup(
        "ordinary group",
        [
            TransportFailure("resposta_invalida", 503),
            OSError(111, "connection refused"),
        ],
    )
    error.__cause__ = ValueError("ordinary cause")
    error.add_note("ordinary primitive note")
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        try:
            _raise(error)
        except ExceptionGroup:
            logger.exception("ordinary operational failure", extra={"password": CANARY})
        client.flush()
    assert len(transport.events) == 1
    event = transport.events[0]
    values = event["exception"]["values"]
    assert {item["type"] for item in values} == {
        "ExceptionGroup",
        "TransportFailureError",
        "ConnectionRefusedError",
        "ValueError",
    }
    assert any(item["value"] == "resposta_invalida" for item in values)
    assert any("ordinary primitive note" in item["value"] for item in values)
    assert any(
        item["mechanism"].get("meta", {}).get("errno", {}).get("number") == 111
        for item in values
    )
    assert any(
        frame["function"] == "_raise"
        for item in values
        for frame in item["stacktrace"]["frames"]
    )
    assert '"vars"' not in json.dumps(event)
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert "resposta_invalida" in stream.getvalue()
    assert event["fingerprint"][0] == "axrm011"
    assert error.exceptions[0].args == ("resposta_invalida",)


def _capture(error: RuntimeError) -> None:
    try:
        _raise(error)
    except RuntimeError:
        sentry_sdk.capture_exception(error)


def test_exception_fingerprint_does_not_depend_on_primitive_argument_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _sinks(monkeypatch) as (_, _, client, transport):
        for code in ("ordinary_code_one", "ordinary_code_two"):
            _capture(RuntimeError(code))
        client.flush()
    assert len(transport.events) == 2
    assert transport.events[0]["fingerprint"] == transport.events[1]["fingerprint"]
    assert transport.events[0]["exception"] != transport.events[1]["exception"]


def test_a_chain_can_cycle_through_group_membership_without_mutating_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("ordinary root")
    group = ExceptionGroup("ordinary group", [error])
    error.__cause__ = group
    with _sinks(monkeypatch) as (_, _, client, transport):
        _capture(error)
        client.flush()
    assert len(transport.events) == 1
    values = transport.events[0]["exception"]["values"]
    assert {item["type"] for item in values} == {"RuntimeError", "ExceptionGroup"}
    assert error.args == ("ordinary root",)
    assert error.__cause__ is group
    assert group.exceptions == (error,)


def test_exception_projection_preserves_crossing_ranges_until_scope_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "synthetic-fix6-private-head-284971"
    known = prefix + " body='abc"
    message = prefix + " body='abc;synthetic-private-tail' status=503"
    with _sinks(monkeypatch) as (_, _, client, transport):
        sentry_sdk.set_extra("password", known)
        _capture(RuntimeError(message))
        client.flush()
    assert len(transport.events) == 1
    delivered = json.dumps(transport.events)
    assert prefix not in delivered
    assert "synthetic-private-tail" not in delivered
    assert "status=503" in delivered


def _emit_stack(logger: logging.Logger, canary: str) -> None:
    logger.error(
        "ordinary stack diagnostic", stack_info=True, extra={"password": canary}
    )


@pytest.mark.parametrize("logs", [False, True])
def test_sdk_stack_snapshots_use_frames_frozen_with_the_record_credentials(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    emit = FunctionType(_emit_stack.__code__.replace(co_filename=CANARY), globals())
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        emit(logger, CANARY)
        client.flush()
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()
    assert "ordinary stack diagnostic" in stream.getvalue()
    assert len(transport.events) == 1
    frames = transport.events[0]["threads"]["values"][0]["stacktrace"]["frames"]
    assert len(frames) > 1
    assert all("vars" not in frame and "context_line" not in frame for frame in frames)


def test_allowlisted_scalars_do_not_call_custom_timezone_code() -> None:
    calls: list[str] = []

    class HostileTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            calls.append("utcoffset")
            message = "custom timezone code executed"
            raise RuntimeError(message)

        def dst(self, dt: datetime | None) -> timedelta | None:
            calls.append("dst")
            message = "custom timezone code executed"
            raise RuntimeError(message)

        def tzname(self, dt: datetime | None) -> str | None:
            calls.append("tzname")
            message = "custom timezone code executed"
            raise RuntimeError(message)

    value = [
        UUID("00000000-0000-0000-0000-000000000001"),
        Decimal("1.25"),
        datetime(2026, 10, 28, tzinfo=UTC),
        datetime(2026, 10, 28, tzinfo=HostileTimezone()),
    ]
    assert project_structure(value) == [
        "00000000-0000-0000-0000-000000000001",
        "1.25",
        "2026-10-28T00:00:00+00:00",
        "2026-10-28T00:00:00",
    ]
    assert calls == []


@pytest.mark.parametrize("logs", [False, True])
def test_hostile_mapping_keys_are_not_hashed_or_compared_during_projection(
    monkeypatch: pytest.MonkeyPatch, logs: bool
) -> None:
    calls: list[str] = []

    class Key:
        def __hash__(self) -> int:
            calls.append("hash")
            return hash("password")

        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            return False

    context: dict[object, object] = {
        Key(): "ordinary retained value",
        "password": CANARY,
    }
    calls.clear()
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        logger.error("ordinary mapping diagnostic %s", context)
        client.flush()
    assert calls == []
    assert "ordinary retained value" in stream.getvalue()
    assert CANARY not in stream.getvalue() + b"".join(transport.payloads).decode()


@pytest.mark.parametrize("declared", [False, True])
def test_the_sdk_celery_ingress_preserves_declared_exception_policy(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    app = Celery("fix6-declared-exception", set_as_current=False)
    task = app.Task()
    type(task).throws = (ValueError,) if declared else ()
    with _sinks(monkeypatch) as (_, _, client, transport):
        try:
            _raise(ValueError("ordinary expected failure"))
        except ValueError:
            sentry_celery._capture_exception(task, sys.exc_info())
        client.flush()
    assert len(transport.events) == (0 if declared else 1)


def test_production_console_is_inert_without_a_sentry_dsn() -> None:
    result = subprocess.run(
        ["./.venv/bin/python", "tests/security/inert_import_probe.py"],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "SENTRY_DSN": "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"calls": []}
    assert "ordinary no-dsn diagnostic" in result.stderr
    assert "synthetic-fix6-no-dsn-credential-456291" not in result.stderr

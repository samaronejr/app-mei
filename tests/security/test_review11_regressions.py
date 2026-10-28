"""The four review-11 failures, through its original real-sink helper."""

from __future__ import annotations

import json
import threading
from contextvars import copy_context
from typing import TYPE_CHECKING, NoReturn

import pytest
import sentry_sdk

from config.settings import prod
from tests.security.test_redaction_boundary_regressions import _sinks

if TYPE_CHECKING:
    import logging
    from contextvars import Context

CANARY = "synthetic-rv11-outer-credential-492713"
STACK_CANARY = "synthetic-rv11-stack-only-284951"


def _raise(error: Exception) -> NoReturn:
    raise error


def _review11_error(
    logger: logging.Logger, shape: str, saved: list[Context]
) -> Exception:
    def echo() -> None:
        logger.error("rv11 nested credential echo %s", CANARY)
        if shape == "thread":
            sentry_sdk.capture_message("rv11 child breadcrumb control")

    class ReentrantError(Exception):
        done = False

        def __getattribute__(self, name: str) -> object:
            if (
                shape in {"args", "__cause__", "__context__"}
                and name == shape
                and not object.__getattribute__(self, "done")
            ):
                object.__setattr__(self, "done", True)
                echo()
            value: object = super().__getattribute__(name)
            return value

        def __str__(self) -> str:
            if not self.done:
                self.done = True
                if shape == "thread":
                    child = threading.Thread(target=echo)
                    child.start()
                    child.join(10)
                    assert not child.is_alive()
                elif shape == "retention":
                    saved.append(copy_context())
            return "ordinary rendered exception"

    return ReentrantError("ordinary argument")


@pytest.mark.parametrize("logs", [False, True])
@pytest.mark.parametrize(
    "shape", ["thread", "args", "__cause__", "__context__", "local", "retention"]
)
def test_review11_no_disclosure_or_copied_context_retention(
    monkeypatch: pytest.MonkeyPatch, shape: str, logs: bool
) -> None:
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        saved: list[Context] = []
        retained: list[str] = []

        def local_failure() -> str:
            password = STACK_CANARY

            class LocalError(Exception):
                done = False

                def __str__(self) -> str:
                    if not self.done:
                        self.done = True
                        logger.error("rv11 local credential echo %s", password)
                    return "ordinary local exception"

            try:
                _raise(LocalError("ordinary local argument"))
            except LocalError:
                logger.exception("rv11 outer local")
            return password

        if shape == "local":
            canary = local_failure()
        else:
            canary = CANARY
            try:
                _raise(_review11_error(logger, shape, saved))
            except Exception:
                logger.exception("rv11 outer diagnostic", extra={"password": CANARY})
        before = stream.getvalue()
        if saved:

            def next_request() -> None:
                with sentry_sdk.isolation_scope() as scope, sentry_sdk.new_scope():
                    scope.clear()
                    scope.set_client(client)
                    retained.extend(prod._known_values())
                    logger.error("rv11 unrelated next request %s", CANARY)
                    sentry_sdk.capture_message("rv11 new request control")

            saved[0].run(next_request)
        sentry_sdk.capture_message("rv11 following control")
        client.flush()
    assert canary not in before + json.dumps(transport.events)
    assert canary not in b"".join(transport.payloads).decode()
    assert CANARY not in retained
    assert prod._known_values() == set()
    assert "rv11 outer" in before
    assert any(event.get("exception", {}).get("values") for event in transport.events)
    assert any(
        event.get("message") == "rv11 following control" for event in transport.events
    )

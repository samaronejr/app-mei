"""Rendering must not reopen a record's credentials or a withheld SDK carrier."""

from __future__ import annotations

import json
import logging
from contextvars import copy_context
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
import sentry_sdk
from sentry_sdk.crons import capture_checkin

from config.settings import prod
from tests.security.test_redaction_boundary_regressions import LOGGER, _sinks

if TYPE_CHECKING:
    from _collections_abc import dict_items
    from collections.abc import Iterator
    from contextvars import Context

CANARY = "synthetic-rv10-record-credential-827461"
CHILD_CANARY = "synthetic-fix5-child-credential-938572"
FOLLOWING = "ordinary following event"
REVIVED = "revived checkin diagnostic"
MONITOR = "ordinary-fix5-monitor"
CHECK_IN_ID = "0" * 32


def _raise(error: Exception) -> NoReturn:
    raise error


class _ReentrantError(Exception):
    def __init__(
        self, logger: logging.Logger, *, copied: bool, depth: int, raises: bool
    ) -> None:
        super().__init__("ordinary attribute-rendered failure")
        self.logger = logger
        self.copied = copied
        self.depth = depth
        self.raises = raises
        self.rendered = False

    def _emit(self) -> None:
        if self.depth > 1:
            try:
                _raise(
                    _ReentrantError(
                        self.logger,
                        copied=self.copied,
                        depth=self.depth - 1,
                        raises=False,
                    )
                )
            except _ReentrantError:
                self.logger.exception(
                    "nested failure credential echo %s",
                    CANARY,
                    extra={"password": CHILD_CANARY},
                )
            # A child's discoveries belong to the complete outer dispatch, not
            # only the child frame; a copied context must keep that union too.
            self.logger.error("retained child credential echo %s", CHILD_CANARY)
        elif self.raises:
            try:
                _raise(RuntimeError("nested raised echo " + CANARY))
            except RuntimeError:
                self.logger.exception("nested raised credential echo %s", CANARY)
        else:
            self.logger.error("reentrant credential echo %s", CANARY)

    def __str__(self) -> str:
        if not self.rendered:
            self.rendered = True
            if self.copied:
                copy_context().run(self._emit)
            else:
                self._emit()
        return "ordinary attribute-rendered failure " + CANARY


@pytest.mark.parametrize("logs", [False, True], ids=["production", "sdk-logs"])
@pytest.mark.parametrize(
    "case",
    ["direct", "copied", "two-level", "nested-raises", "chain-only", "rendered-label"],
)
def test_reentrant_exception_rendering_protects_the_complete_dispatch(
    monkeypatch: pytest.MonkeyPatch, case: str, logs: bool
) -> None:
    # Given record-local credentials and the actual console/full SDK options.
    with _sinks(monkeypatch, logs=logs) as (logger, stream, client, transport):
        error = _ReentrantError(
            logger,
            copied=case in {"copied", "two-level"},
            depth=2 if case == "two-level" else 1,
            raises=case == "nested-raises",
        )
        extra = {} if case in {"chain-only", "rendered-label"} else {"password": CANARY}
        if case == "chain-only":
            error.__cause__ = RuntimeError({"password": CANARY})
        # When formatting itself emits another diagnostic, possibly reentrantly
        # in a copied Context and with a different nested exception.
        try:
            _raise(error)
        except _ReentrantError:
            if case == "rendered-label":
                logger.exception("outer record-local failure password=%s", CANARY)
            else:
                logger.exception("outer record-local failure", extra=extra)
        known_after = prod._known_values()
        sentry_sdk.capture_message(FOLLOWING)
        client.flush()
    # Then every sink is safe, without invoking the application's renderer.
    console = stream.getvalue()
    delivered = b"".join(transport.payloads).decode()
    events = json.dumps(transport.events)
    assert all(
        value not in console + delivered + events for value in (CANARY, CHILD_CANARY)
    )
    assert "outer record-local failure" in console
    assert "credential echo" not in console
    assert error.rendered is False
    assert "log record withheld" not in console
    assert known_after == set()
    outer = next(event for event in transport.events if event.get("exception"))
    assert outer.get("exception", {}).get("values")
    later = next(
        event for event in transport.events if event.get("message") == FOLLOWING
    )
    assert "credential echo" not in json.dumps(later.get("breadcrumbs"))
    assert "outer record-local failure" in delivered
    if logs:
        assert "log" in transport.item_types


class _CopyingRequest:
    def __init__(self) -> None:
        self.context: Context | None = None

    @property
    def path_info(self) -> str:
        if self.context is None:
            self.context = copy_context()
        return "/fix5-ordinary-unmatched-route/"


class _RenderingField:
    def __init__(self, logger: logging.Logger, request: _CopyingRequest) -> None:
        self.logger = logger
        self.request = request
        self.rendered = False

    def __str__(self) -> str:
        if not self.rendered:
            self.rendered = True
            context = self.request.context
            if context is None:
                context = copy_context()
            context.run(self.logger.error, "reentrant field credential echo %s", CANARY)
        return "ordinary rendered field " + CANARY

    def __repr__(self) -> str:
        return str(self)


@pytest.mark.parametrize(
    "field", ["msg", "args", "levelname", "stack_info", "exc_text"]
)
@pytest.mark.parametrize("copied", ["during-render", "before-discovery"])
def test_raw_record_discovery_precedes_every_rendering_field(
    monkeypatch: pytest.MonkeyPatch, field: str, copied: str
) -> None:
    # Given a password extra that appears after a rendering field in the record.
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        record = logging.LogRecord(
            LOGGER, logging.ERROR, __file__, 0, "ordinary record field", (), None
        )
        request = _CopyingRequest()
        value = _RenderingField(logger, request)
        record.__dict__[field] = (value,) if field == "args" else value
        if field == "args":
            record.msg = "ordinary record field %s"
        record.__dict__["password"] = CANARY
        if copied == "before-discovery":
            record.__dict__["request"] = request
        # When message interpolation, a standard attribute or a stack renders.
        logger.handle(record)
        sentry_sdk.capture_message(FOLLOWING)
        client.flush()
    # Then the nested diagnostic inherited discovery, even through copy_context.
    emitted = stream.getvalue() + b"".join(transport.payloads).decode()
    assert CANARY not in emitted + json.dumps(transport.events)
    assert "ordinary record field" in emitted or "_RenderingField" in emitted
    assert "reentrant field credential echo" not in stream.getvalue()
    assert value.rendered is False
    assert request.context is None
    assert "log record withheld" not in stream.getvalue()
    assert any(event.get("message") == FOLLOWING for event in transport.events)
    assert "log" in transport.item_types
    assert prod._known_values() == set()


class _RevivingCarrier(dict[str, object]):
    def __init__(self, *, accessor: str, withheld: bool, checkin: bool) -> None:
        self.accessor = accessor
        self.withheld = withheld
        self.reads = 0
        self.override_calls = 0
        self.offered: dict[str, object] = {
            "event_id": "1" * 32,
            "message": REVIVED + " " + CANARY,
            "level": "error",
        }
        if checkin:
            self.offered.update(
                type="check_in",
                monitor_slug=CANARY,
                status="ok",
                check_in_id=CHECK_IN_ID,
            )
        stored = {
            **self.offered,
            "message": "ordinary initial carrier",
            "monitor_slug": MONITOR,
            "extra": {"password": CANARY},
        }
        super().__init__({prod._WITHHELD_EVENT: True} if withheld else stored)

    def items(self) -> dict_items[str, object]:
        self.override_calls += 1
        self.reads += 1
        if not self.withheld and self.reads == 1:
            message = "the carrier refuses the initial scrub"
            raise RuntimeError(message)
        if self.accessor == "get":
            return {name: self.get(name) for name in self.offered}.items()
        if self.accessor == "iter":
            return {name: self[name] for name in self}.items()
        return self.offered.items()

    def get(self, key: str, default: object = None) -> object:
        self.override_calls += 1
        if key == prod._WITHHELD_EVENT:
            return False
        if dict.get(self, prod._WITHHELD_EVENT):
            if self.accessor == "get-raises":
                message = "the husked carrier refuses SDK introspection"
                raise RuntimeError(message)
            return self.offered.get(key, default)
        return dict.get(self, key, default)

    def __iter__(self) -> Iterator[str]:
        self.override_calls += 1
        return iter(self.offered)

    def __getitem__(self, key: str) -> object:
        self.override_calls += 1
        return self.offered[key]


@pytest.mark.parametrize("accessor", ["items", "get", "iter", "get-raises"])
@pytest.mark.parametrize("withheld", [False, True], ids=["fresh", "husk"])
@pytest.mark.parametrize(
    "capture", ["check-in", "scope-less-check-in", "scope-less-event"]
)
def test_deceptive_carriers_never_revive_a_husk_or_escape_capture(
    monkeypatch: pytest.MonkeyPatch, accessor: str, withheld: bool, capture: str
) -> None:
    # Given a carrier that would refuse discovery and then revive an unsafe event,
    # or that already stores only the irrevocable withheld marker.
    event: Any = _RevivingCarrier(
        accessor=accessor, withheld=withheld, checkin=capture != "scope-less-event"
    )
    with _sinks(monkeypatch, logs=True) as (logger, stream, client, transport):
        # When either public scoped capture or direct scope-less capture sees it.
        if capture == "check-in":
            event_id = sentry_sdk.capture_event(event)
        else:
            event_id = client.capture_event(event, scope=None)
        logger.error("ordinary carrier control")
        sentry_sdk.capture_message(FOLLOWING)
        client.flush()
    # Then only base storage was read; no original accessor, revival or exception.
    delivered = b"".join(transport.payloads).decode()
    assert CANARY not in stream.getvalue() + delivered + json.dumps(transport.events)
    assert REVIVED not in delivered
    assert event.override_calls == 0
    assert dict(dict.items(event)) == {prod._WITHHELD_EVENT: True}
    if withheld:
        assert event_id is None
    else:
        assert event_id is not None
        assert "ordinary initial carrier" in delivered
    assert "ordinary carrier control" in stream.getvalue()
    assert any(item.get("message") == FOLLOWING for item in transport.events)
    assert "log" in transport.item_types


class _UnreadableMonitorConfig(dict[str, object]):
    def items(self) -> dict_items[str, object]:
        message = "monitor configuration refuses discovery"
        raise RuntimeError(message)


def test_a_withheld_checkin_does_not_destroy_the_public_checkin_return_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a check-in whose private configuration fails raw credential discovery.
    config: Any = _UnreadableMonitorConfig(password=CANARY)
    with _sinks(monkeypatch) as (_, _, client, transport):
        # When the public cron API reads its return ID after the client drops it.
        check_in_id = capture_checkin(
            monitor_slug=MONITOR,
            check_in_id=CHECK_IN_ID,
            status="ok",
            monitor_config=config,
        )
        sentry_sdk.capture_message(FOLLOWING)
        client.flush()
    # Then nothing escapes the caller, the check-in stays dropped, and events flow.
    assert check_in_id == CHECK_IN_ID
    assert "check_in" not in transport.item_types
    assert CANARY not in b"".join(transport.payloads).decode()
    assert [event.get("message") for event in transport.events] == [FOLLOWING]

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import sentry_sdk
from sentry_sdk.transport import Transport

from config.settings import prod as prod_settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sentry_sdk.envelope import Envelope
    from sentry_sdk.types import Event

SENTRY_TEST_DSN = "http://public@example.invalid/1"
CANARY_LOCAL_VALUE = "PILOT504-CANARY"
EXCEPTION_MESSAGE = "PILOT-504 synthetic worker failure"


class _CaptureTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            event = item.get_event()
            if event is not None:
                self.events.append(event)


def _raise_with_canary() -> None:
    canary_local_value = CANARY_LOCAL_VALUE
    assert canary_local_value
    raise RuntimeError(EXCEPTION_MESSAGE)


def _capture_exception_event(options: Mapping[str, Any]) -> Event:
    transport = _CaptureTransport()
    event_id: str | None = None
    client = sentry_sdk.Client(
        **options,
        dsn=SENTRY_TEST_DSN,
        transport=transport,
    )
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            try:
                _raise_with_canary()
            except RuntimeError as error:
                event_id = scope.capture_exception(error)
            client.flush()
    finally:
        client.close()

    assert event_id is not None
    assert len(transport.events) == 1
    return transport.events[0]


def _exception_and_frames(event: Event) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    exception_data = event.get("exception")
    assert isinstance(exception_data, dict)
    exception = cast("dict[str, Any]", exception_data)
    values = cast("list[dict[str, Any]]", exception["values"])
    exception_value = values[0]
    stacktrace = cast("dict[str, Any]", exception_value["stacktrace"])
    frames = cast("list[dict[str, Any]]", stacktrace["frames"])
    return exception_value, frames


def test_production_sentry_omits_frame_locals_without_emptying_traceback() -> None:
    production_event = _capture_exception_event(prod_settings.SENTRY_OPTIONS)
    exception, frames = _exception_and_frames(production_event)

    assert frames
    assert all(
        isinstance(frame.get("filename"), str)
        and isinstance(frame.get("function"), str)
        and isinstance(frame.get("lineno"), int)
        for frame in frames
    )
    raising_frame = next(
        frame for frame in frames if frame["function"] == "_raise_with_canary"
    )
    assert Path(raising_frame["filename"]).name == Path(__file__).name
    assert exception["type"] == "RuntimeError"
    assert exception["value"] == EXCEPTION_MESSAGE
    assert all("vars" not in frame for frame in frames)
    assert CANARY_LOCAL_VALUE not in json.dumps(production_event)

    # Negative control: the same event must expose the planted value when enabled.
    control_event = _capture_exception_event(
        {**prod_settings.SENTRY_OPTIONS, "include_local_variables": True},
    )
    _, control_frames = _exception_and_frames(control_event)
    control_raising_frame = next(
        frame for frame in control_frames if frame["function"] == "_raise_with_canary"
    )
    control_vars = cast("dict[str, Any]", control_raising_frame["vars"])
    assert CANARY_LOCAL_VALUE in str(control_vars["canary_local_value"])

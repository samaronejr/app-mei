"""The screenshot capture retries one Chromium protocol error once, and nothing else.

T90/AXRM-003: the header-fit matrix failed intermittently under gate load with
`Protocol error (Page.captureScreenshot): Unable to capture screenshot`. The capture
retries exactly that error, once, and keeps the first failure as an artifact; an
assertion, a timeout or any other error is never retried.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.browser.harness import (
    CAPTURE_PROTOCOL_ERROR,
    capture_retrying_protocol_error,
)

STATE = "url=http://acme.localhost/ viewport={'width': 1440, 'height': 900}"


class ScriptedCapture:
    """A capture call that raises each scripted outcome in turn, then succeeds."""

    def __init__(self, *outcomes: BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.outcomes:
            raise self.outcomes.pop(0)


def protocol_error() -> PlaywrightError:
    return PlaywrightError(f"{CAPTURE_PROTOCOL_ERROR}\nCall log:\n  - fonts loaded")


def test_the_protocol_error_is_retried_once_and_kept(tmp_path: Path) -> None:
    capture = ScriptedCapture(protocol_error())
    artifact = tmp_path / "shot-desktop.capture-error.txt"

    capture_retrying_protocol_error(capture, artifact, STATE)

    assert capture.calls == 2
    kept = artifact.read_text(encoding="utf-8")
    assert CAPTURE_PROTOCOL_ERROR in kept
    assert STATE in kept


def test_a_second_protocol_error_fails_the_test(tmp_path: Path) -> None:
    second = protocol_error()
    capture = ScriptedCapture(protocol_error(), second)
    artifact = tmp_path / "shot-desktop.capture-error.txt"

    with pytest.raises(PlaywrightError) as raised:
        capture_retrying_protocol_error(capture, artifact, STATE)

    assert raised.value is second
    assert capture.calls == 2
    assert artifact.exists()


@pytest.mark.parametrize(
    "failure",
    [
        AssertionError("geometry mismatch"),
        PlaywrightTimeoutError("Timeout 30000ms exceeded."),
        PlaywrightError("Target page, context or browser has been closed"),
        PlaywrightError("Protocol error (Page.captureScreenshot): Target closed"),
    ],
    ids=["assertion", "timeout", "target-closed", "other-capture-protocol-error"],
)
def test_nothing_else_is_retried(tmp_path: Path, failure: BaseException) -> None:
    capture = ScriptedCapture(failure)
    artifact = tmp_path / "shot-desktop.capture-error.txt"

    with pytest.raises(type(failure)) as raised:
        capture_retrying_protocol_error(capture, artifact, STATE)

    assert raised.value is failure
    assert capture.calls == 1
    assert not artifact.exists()


def test_a_clean_capture_runs_once_and_keeps_nothing(tmp_path: Path) -> None:
    capture = ScriptedCapture()
    artifact = tmp_path / "shot-desktop.capture-error.txt"

    capture_retrying_protocol_error(capture, artifact, STATE)

    assert capture.calls == 1
    assert not artifact.exists()

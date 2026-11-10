"""AXRM-018: a client whose own claim crashes is a failed client.

Plan todo 18: "heartbeat not stamped on failure". The real `payments_for` and
`report_for` run here; only their opening call raises an unexpected error for the
first visited client while the others are served. That client must not be classified
as `EXISTING` (a consultation already open) or `REFUSED` (a guard declined): the run
keeps `federal_sweep_failed`, leaves `last_success_at` unset, logs the warning with
its traceback and sends that same exception to Sentry.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.integrations.serpro import tasks
from apps.integrations.serpro.models import PaymentQueryState
from apps.obligations.models import SchedulerHeartbeat
from tests.integrations.test_federal_sweeps import (
    DAY,
    PAYMENTS,
    firms,
    lane,
    on,
    sweeps,
)

__all__ = ["lane"]
pytestmark = pytest.mark.django_db(transaction=True)

SITFIS = "schedule_situation_reports"
FAILED = "federal_sweep_failed"


class CrashError(RuntimeError):
    """An unexpected error from the opening call (not a refusal, not a conflict)."""


def _observe(monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], list[object]]:
    """Record the module's warnings and Sentry captures; logging config is untouched."""
    module = sweeps()
    warnings: list[object] = []
    captured: list[object] = []

    def warning(message: str, *args: object, **kwargs: object) -> None:
        warnings.append((message, kwargs.get("exc_info")))

    monkeypatch.setattr(module.logger, "warning", warning)
    monkeypatch.setattr(module.sentry_sdk, "capture_exception", captured.append)
    return warnings, captured


def _assert_failed_run(
    name: str, crashes: list[CrashError], warnings: list[object], captured: list[object]
) -> None:
    beat = SchedulerHeartbeat.objects.get(name=name)
    assert beat.last_success_at is None
    assert beat.last_error_code == FAILED
    assert len(crashes) == 1
    assert warnings == [("federal sweep failed for one client", True)]
    assert len(captured) == 1
    assert captured[0] is crashes[0]


def test_a_consultation_that_crashes_while_opening_is_a_failed_client(
    lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = sweeps()
    firms(1, 3)
    opened: list[object] = []
    crashes: list[CrashError] = []
    handed: list[tuple[object, ...]] = []

    def open_query(client, window, *, actor, cadence):  # noqa: ANN001, ANN202
        opened.append(client.pk)
        if len(opened) == 1:
            crash = CrashError("provider down")
            crashes.append(crash)
            raise crash
        return SimpleNamespace(
            state=PaymentQueryState.NA_FILA, tenant_id=client.tenant_id, pk=uuid4()
        )

    warnings, captured = _observe(monkeypatch)
    monkeypatch.setattr(module, "termo_parties", lambda connection_id, cnpj: object())
    monkeypatch.setattr(
        module,
        "authority_for",
        lambda client, operation, link: SimpleNamespace(usable=True, state=None),
    )
    monkeypatch.setattr(module, "open_payment_query", open_query)
    monkeypatch.setattr(
        tasks.run_payment_query_task, "delay", lambda *args: handed.append(args)
    )
    on(monkeypatch, DAY)
    queued = getattr(tasks, PAYMENTS).apply().get()
    assert (queued, len(opened), len(handed)) == (2, 3, 2)
    _assert_failed_run(PAYMENTS, crashes, warnings, captured)


def test_a_report_request_that_crashes_is_a_failed_client(
    lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = sweeps()
    firms(1, 3)
    asked: list[object] = []
    crashes: list[CrashError] = []

    def request(client):  # noqa: ANN001, ANN202
        asked.append(client.pk)
        if len(asked) == 1:
            crash = CrashError("provider down")
            crashes.append(crash)
            raise crash

    warnings, captured = _observe(monkeypatch)
    monkeypatch.setattr(module, "report_candidates", lambda today: module._linked())
    monkeypatch.setattr(module, "request_situation_report", request)
    on(monkeypatch, DAY)
    queued = getattr(tasks, SITFIS).apply().get()
    assert (queued, len(asked)) == (2, 3)
    _assert_failed_run(SITFIS, crashes, warnings, captured)

"""AXRM-018: each sweep stamps its own heartbeat and leaves the other sweep's alone.

The automations page reads each federal job's health from the heartbeat named after its
task. A run of one sweep must create or update only that sweep's heartbeat; the other
sweep's row must not appear, and an existing one must keep its stamps.
"""

import pytest

from apps.clients.models import ClientCompany
from apps.integrations.serpro import tasks
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


def _served(monkeypatch: pytest.MonkeyPatch) -> None:
    module = sweeps()
    queued = module.Outcome.QUEUED

    def payment(client: ClientCompany, window: object) -> object:
        return queued

    def report(client: ClientCompany) -> object:
        return queued

    monkeypatch.setattr(module, "payments_for", payment)
    monkeypatch.setattr(module, "report_for", report)
    monkeypatch.setattr(module, "report_candidates", lambda today: module._linked())
    on(monkeypatch, DAY)


@pytest.mark.parametrize(("kind", "other"), [(SITFIS, PAYMENTS), (PAYMENTS, SITFIS)])
def test_a_sweep_stamps_only_its_own_heartbeat(
    lane: None, monkeypatch: pytest.MonkeyPatch, kind: str, other: str
) -> None:
    firms(1, 2)
    _served(monkeypatch)
    assert getattr(tasks, kind).apply().get() == 2
    own = SchedulerHeartbeat.objects.filter(name=kind).first()
    assert own is not None
    assert own.last_success_at is not None
    assert own.last_error_code == ""
    assert not SchedulerHeartbeat.objects.filter(name=other).exists()

    before = SchedulerHeartbeat.objects.get(name=kind)
    assert getattr(tasks, other).apply().get() == 2
    after = SchedulerHeartbeat.objects.get(name=kind)
    assert (after.last_attempt_at, after.last_success_at) == (
        before.last_attempt_at,
        before.last_success_at,
    )
    assert SchedulerHeartbeat.objects.get(name=other).last_success_at is not None

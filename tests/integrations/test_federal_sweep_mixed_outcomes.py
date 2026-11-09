"""AXRM-018: a run where some clients fail and others are served never shows success.

Plan todo 18: "heartbeat not stamped on failure". The automations page reads
`federal_sweep_failed` as "falha em ao menos um cliente. Os demais foram atendidos",
so a mixed run must keep that code and leave `last_success_at` unset, wherever the
failure lands: first, middle or last client of a firm's turn, and in the first or
last firm visited. Both monthly sweeps share `_sweep`; each runs its real beat task.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ["lane"]
pytestmark = pytest.mark.django_db(transaction=True)

SITFIS = "schedule_situation_reports"
FAILED = "federal_sweep_failed"


def _fire(
    monkeypatch: pytest.MonkeyPatch, kind: str, act: Callable[[ClientCompany], object]
) -> int:
    """Run one sweep kind with `act` as its per-client action; return what it queued."""
    module = sweeps()
    if kind == PAYMENTS:
        monkeypatch.setattr(module, "payments_for", lambda client, window: act(client))
    else:
        # Any linked client is a SITFIS candidate here; the action is replaced anyway.
        monkeypatch.setattr(module, "report_candidates", lambda today: module._linked())
        monkeypatch.setattr(module, "report_for", act)
    on(monkeypatch, DAY)
    return int(getattr(tasks, kind).apply().get())


def _beat(kind: str) -> SchedulerHeartbeat:
    return SchedulerHeartbeat.objects.get(name=kind)


@pytest.mark.parametrize("kind", [PAYMENTS, SITFIS])
@pytest.mark.parametrize("failing", [0, 1, 2], ids=["first", "middle", "last"])
def test_one_failed_client_among_served_ones_keeps_the_failure_code(
    lane: None, monkeypatch: pytest.MonkeyPatch, kind: str, failing: int
) -> None:
    firms(1, 3)
    queued = sweeps().Outcome.QUEUED
    seen: list[UUID] = []

    def flaky(client: ClientCompany) -> object:
        seen.append(client.pk)
        if len(seen) - 1 == failing:
            message = "provider down"
            raise RuntimeError(message)
        return queued

    served = _fire(monkeypatch, kind, flaky)
    beat = _beat(kind)
    assert (served, len(seen)) == (2, 3)
    assert beat.last_success_at is None
    assert beat.last_error_code == FAILED
    assert beat.last_attempt_at is not None


@pytest.mark.parametrize("kind", [PAYMENTS, SITFIS])
@pytest.mark.parametrize("failing_firm", [0, 1], ids=["first-firm", "last-firm"])
def test_a_failed_firm_beside_a_fully_served_firm_keeps_the_failure_code(
    lane: None, monkeypatch: pytest.MonkeyPatch, kind: str, failing_firm: int
) -> None:
    firms(2, 2)
    queued = sweeps().Outcome.QUEUED
    order: list[UUID] = []

    def by_firm(client: ClientCompany) -> object:
        if client.tenant_id not in order:
            order.append(client.tenant_id)
        if order.index(client.tenant_id) == failing_firm:
            message = "provider down"
            raise RuntimeError(message)
        return queued

    served = _fire(monkeypatch, kind, by_firm)
    beat = _beat(kind)
    assert len(order) == 2
    assert served == 2
    assert beat.last_success_at is None
    assert beat.last_error_code == FAILED

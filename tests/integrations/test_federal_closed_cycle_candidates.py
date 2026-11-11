"""Closed-cycle clients do not own a new monthly report turn."""

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.obligations.models import FiscalSituationReport
from tests.integrations.sitfis_support import World, world
from tests.integrations.test_federal_sweeps_sitfis import clock, fire, queued

__all__ = ["clock", "queued", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "state", [CycleState.COMPLETED, CycleState.WITH_PENDING, CycleState.CANCELLED]
)
def test_a_closed_cycle_does_not_request_a_monthly_report(
    world: World,
    queued: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    with tenant_context(world.tenant_id):
        ServiceCycle.objects.filter(pk=world.cycle.pk).update(state=state)
    assert fire(monkeypatch) == 0
    assert queued == []
    with tenant_context(world.tenant_id):
        assert not FiscalSituationReport.objects.exists()

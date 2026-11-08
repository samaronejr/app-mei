"""AXRM-018: a month's report in any final state is never requested again.

Plan todo 18: request a report for each client whose cycle opened this month "and
has none yet", and never retry billed failures automatically. Every final state
this month, failed or not, must keep the sweep from opening another report or
handing another delivery to the worker.
"""

from datetime import date

import pytest

from apps.core.tenancy import tenant_context
from apps.obligations.models import FiscalSituationReport
from tests.integrations.sitfis_support import World, world
from tests.integrations.test_federal_sweeps_sitfis import clock, fire, queued, reports

__all__ = ["clock", "queued", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "final",
    [
        "falha",
        "negado",
        "incerto",
        "protocolo_expirado",
        "limite_de_tentativas",
        "emitido",
    ],
)
def test_a_final_report_this_month_is_not_requested_again(
    world: World,
    queued: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    final: str,
) -> None:
    assert fire(monkeypatch) == 1
    with tenant_context(world.tenant_id):
        FiscalSituationReport.objects.update(state=final)
    assert [fire(monkeypatch), fire(monkeypatch, date(2026, 10, 16))] == [0, 0]
    assert reports(world) == 1
    assert len(queued) == 1

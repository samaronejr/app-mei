"""Small real source graphs for the briefing contracts."""

from datetime import UTC, date, datetime, timedelta

from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def briefing_setup(slug: str = "briefing-contract") -> CycleSetup:
    setup = cycle_setup(slug)
    setup.tenant.features = ["cycles_r1", "briefing_r1"]
    setup.tenant.save(update_fields=["features"])
    return setup


def due_obligation(
    setup: CycleSetup, *, period: date = PERIOD, days: int = 2
) -> Obligation:
    with tenant_context(setup.tenant.pk):
        return Obligation.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            obligation_type_id="DAS",
            competence_month=period,
            nominal_due_date=NOW.date() + timedelta(days=days),
            resolved_due_date=NOW.date() + timedelta(days=days),
            status=ObligationStatus.DUE,
        )

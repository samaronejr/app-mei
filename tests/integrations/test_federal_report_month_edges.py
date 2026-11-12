"""Received reports consume their slot from the exact local month boundary."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.serpro import sweeps
from apps.obligations.models import FiscalSituationReport
from tests.integrations.sitfis_support import World, world
from tests.integrations.test_federal_sweeps_sitfis import clock

__all__ = ["clock", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_received_report_at_exact_local_month_start_is_not_requested_again(
    world: World,
) -> None:
    report = world.request()
    first = timezone.make_aware(datetime.combine(date(2026, 10, 1), time.min))
    with tenant_context(world.tenant_id):
        FiscalSituationReport.objects.filter(pk=report.pk).update(
            state="protocolo_recebido", requested_at=first
        )
        assert (
            not sweeps.report_candidates(date(2026, 10, 21))
            .filter(pk=world.termo.client.pk)
            .exists()
        )


@pytest.mark.parametrize("zone", ["UTC", "America/Sao_Paulo"])
@pytest.mark.parametrize("state", ["solicitado", "protocolo_recebido", "emitido"])
@pytest.mark.parametrize(
    ("edge", "before_month"),
    [
        ("previous-first", True),
        ("previous-last", True),
        ("first", False),
        ("first-plus-microsecond", False),
        ("first-plus-day", False),
        ("last", False),
    ],
)
def test_received_report_month_edges_in_utc_and_local_time(
    world: World, zone: str, state: str, edge: str, before_month: bool
) -> None:
    report = world.request()
    tz = ZoneInfo(zone)
    first = datetime(2026, 10, 1, tzinfo=tz)
    instants = {
        "previous-first": datetime(2026, 9, 1, tzinfo=tz),
        "previous-last": first - timedelta(microseconds=1),
        "first": first,
        "first-plus-microsecond": first + timedelta(microseconds=1),
        "first-plus-day": first + timedelta(days=1),
        "last": datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=tz),
    }
    # Store the same absolute instant as UTC; selection uses the active local zone.
    requested = instants[edge].astimezone(UTC)
    expected = (
        {world.termo.client.pk} if before_month or state == "solicitado" else set()
    )
    with timezone.override(tz), tenant_context(world.tenant_id):
        FiscalSituationReport.objects.filter(pk=report.pk).update(
            state=state, requested_at=requested
        )
        selected = set(
            sweeps.report_candidates(date(2026, 10, 21)).values_list("pk", flat=True)
        )
        assert selected == expected

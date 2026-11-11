"""Monthly report delivery ownership across worker completion and later cycles."""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.services.cycle import open_cycle
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.serpro import sitfis_tasks, tasks
from apps.obligations.models import FiscalSituationReport
from tests.integrations.sitfis_support import (
    Clock,
    World,
    protocol_reply,
    report_reply,
    world,
)

__all__ = ["clock", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


# Real-clock model defaults (a provider call's `next_attempt_at`) are bound when
# the module is imported and are never patched, so the fixture instants must stay
# ahead of real time by days, whatever date the suite runs on: the 8th of next
# month at 15:00Z, then the 1st of the month after, which is 21 to 24 days later
# and inside the admitted termo's 30 days.
_REAL = datetime.now(UTC)
FIRST = (
    (_REAL.replace(day=28) + timedelta(days=4)).replace(day=1) + timedelta(days=7)
).replace(hour=15, minute=0, second=0, microsecond=0)
SECOND = (FIRST.replace(day=28) + timedelta(days=4)).replace(day=1)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    current = Clock(FIRST)
    monkeypatch.setattr(timezone, "now", current.now)
    return current


def test_report_delivery_is_once_per_month_after_owned_worker_completion(
    world: World,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN18: "monthly checks run without manual effort".

    Once the worker owns a report, later sweep turns must not publish it again.
    A new month's cycle must still get its own report.
    """
    deliveries: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sitfis_tasks.advance_situation_report_task,
        "delay",
        lambda tenant_id, report_id: deliveries.append((tenant_id, report_id)),
    )
    report_ids = []
    tariff_revision = world.budget().tariff_revision
    for opened in (FIRST, SECOND):
        # The admitted termo lasts 30 days from the first instant. Both months'
        # consultations must run while that real admission remains valid.
        year, month, day = opened.year, opened.month, opened.day
        clock.current = opened
        with tenant_context(world.tenant_id):
            # Admission requires a funded budget row for the actual call month.
            ProviderBudget.objects.get_or_create(
                provider="serpro",
                window_start=date(year, month, 1),
                defaults={
                    "tenant_id": world.tenant_id,
                    "cap_microbrl": 2_000_000,
                    "tariff_revision": tariff_revision,
                },
            )
            open_cycle(
                world.termo.actor,
                world.cycle.engagement_id,
                date(year, month, 1),
                world.cycle.playbook_version_id,
                idempotency_key=f"axrm018-month-{year}-{month}",
            )
        result = tasks.schedule_situation_reports.apply()
        assert result.successful(), result.result
        assert result.get() == 1
        with tenant_context(world.tenant_id):
            report = FiscalSituationReport.objects.get(
                requested_at__year=year, requested_at__month=month
            )
        report_ids.append(report.pk)
        world.script(protocol_reply(), report_reply())
        advanced = world.advance(report)
        assert advanced.state == "aguardando", [
            (call.state, call.provider_code, call.http_status)
            for call in world.calls("SP-SIT-P")
        ]
        clock.advance(seconds=31)
        advanced = world.advance(report)
        assert advanced.state == "emitido"
        for later_day in (day + 1, day + 2):
            clock.current = opened.replace(day=later_day)
            result = tasks.schedule_situation_reports.apply()
            assert result.successful(), result.result
            assert result.get() == 0
        assert len(deliveries) == len(report_ids)
    assert len(set(report_ids)) == 2
    assert deliveries == [(str(world.tenant_id), str(pk)) for pk in report_ids]
    assert len(world.sent("/Apoiar")) == 2
    assert len(world.sent("/Emitir")) == 2

"""Monthly report delivery ownership across worker completion and later cycles."""

from datetime import UTC, date, datetime

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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    current = Clock(datetime(2026, 11, 8, 15, tzinfo=UTC))
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
    for month in (11, 12):
        # The admitted termo lasts 30 days from November 8. Both months'
        # consultations must run while that real admission remains valid.
        day = 8 if month == 11 else 1
        clock.current = datetime(2026, month, day, 15, tzinfo=UTC)
        with tenant_context(world.tenant_id):
            # Admission requires a funded budget row for the actual call month.
            ProviderBudget.objects.get_or_create(
                provider="serpro",
                window_start=date(2026, month, 1),
                defaults={
                    "tenant_id": world.tenant_id,
                    "cap_microbrl": 2_000_000,
                    "tariff_revision": tariff_revision,
                },
            )
            open_cycle(
                world.termo.actor,
                world.cycle.engagement_id,
                date(2026, month, 1),
                world.cycle.playbook_version_id,
                idempotency_key=f"axrm018-month-{month}",
            )
        result = tasks.schedule_situation_reports.apply()
        assert result.successful(), result.result
        assert result.get() == 1
        with tenant_context(world.tenant_id):
            report = FiscalSituationReport.objects.get(
                requested_at__year=2026, requested_at__month=month
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
            clock.current = datetime(2026, month, later_day, 15, tzinfo=UTC)
            result = tasks.schedule_situation_reports.apply()
            assert result.successful(), result.result
            assert result.get() == 0
        assert len(deliveries) == len(report_ids)
    assert len(set(report_ids)) == 2
    assert deliveries == [(str(world.tenant_id), str(pk)) for pk in report_ids]
    assert len(world.sent("/Apoiar")) == 2
    assert len(world.sent("/Emitir")) == 2

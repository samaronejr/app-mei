"""Worker entry point for SITFIS; no provider I/O ever runs in a web request.

AXRM-016. The task re-reads the durable report on every delivery, so a
redelivered, duplicated or late message is harmless: it advances the report as
far as the clock allows and schedules itself for the next moment it could act.

A delivery that raises (a database or storage outage) is not the end of the
report: it schedules a bounded continuation before re-raising, and once the
retries are spent it opens owned work for the report. A continuation never
resends a paid emission: each attempt's identity is fixed by the report's rows.
"""

from datetime import timedelta
from typing import Final
from uuid import UUID

from celery import shared_task
from django.utils import timezone

from apps.core.tasks import PhasedTenantTask
from apps.integrations.serpro.sitfis import (
    advance_situation_report,
    next_run_at,
    own_stalled_report,
)

# After the first, second and third consecutive failed delivery.
RETRY_DELAYS: Final = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
)


def _advance(tenant_id: str, report_id: str, failures: int = 0) -> str:
    """Advance one report; emits SP-SIT-R only at or after its `wait_until`."""
    try:
        report = advance_situation_report(UUID(tenant_id), UUID(report_id))
    except Exception:
        _after_failure(tenant_id, report_id, failures)
        raise
    eta = next_run_at(report)
    if eta is not None:
        # Nobody waits for the continuation: no result is subscribed to or kept.
        advance_situation_report_task.apply_async(
            (tenant_id, report_id), eta=eta, ignore_result=True
        )
    return report.state


def _after_failure(tenant_id: str, report_id: str, failures: int) -> None:
    """Continue the report later, or own it once the retries are spent."""
    if failures < len(RETRY_DELAYS):
        advance_situation_report_task.apply_async(
            (tenant_id, report_id),
            {"failures": failures + 1},
            eta=timezone.now() + RETRY_DELAYS[failures],
            ignore_result=True,
        )
        return
    own_stalled_report(UUID(tenant_id), UUID(report_id))


advance_situation_report_task = shared_task(
    name="apps.integrations.serpro.sitfis_tasks.advance_situation_report_task",
    base=PhasedTenantTask,
)(_advance)

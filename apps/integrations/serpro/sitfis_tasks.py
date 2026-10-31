"""Worker entry point for SITFIS; no provider I/O ever runs in a web request.

AXRM-016. The task re-reads the durable report on every delivery, so a
redelivered, duplicated or late message is harmless: it advances the report as
far as the clock allows and schedules itself for the next moment it could act.
"""

from uuid import UUID

from celery import shared_task

from apps.core.tasks import PhasedTenantTask
from apps.integrations.serpro.sitfis import advance_situation_report, next_run_at


def _advance(tenant_id: str, report_id: str) -> str:
    """Advance one report; emits SP-SIT-R only at or after its `wait_until`."""
    report = advance_situation_report(UUID(tenant_id), UUID(report_id))
    eta = next_run_at(report)
    if eta is not None:
        # Nobody waits for the continuation: no result is subscribed to or kept.
        advance_situation_report_task.apply_async(
            (tenant_id, report_id), eta=eta, ignore_result=True
        )
    return report.state


advance_situation_report_task = shared_task(
    name="apps.integrations.serpro.sitfis_tasks.advance_situation_report_task",
    base=PhasedTenantTask,
)(_advance)

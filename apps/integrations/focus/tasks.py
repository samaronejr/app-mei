"""Hourly platform capture; each stream owns its short database phases."""

from celery import Task, shared_task

from apps.core.tasks import PlatformTask
from apps.integrations.focus.capture import sweep


@shared_task(base=PlatformTask, bind=True)
def capture_documents(_task: Task) -> int:
    """Visit enabled tenants fairly; capture refuses every closed control."""
    return sweep()

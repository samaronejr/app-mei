"""Shadow classification has no user-visible output and no effect execution."""

from uuid import UUID

import sentry_sdk
from celery import shared_task
from celery.exceptions import OperationalError
from django.utils import timezone

from apps.automation.admission import eligible
from apps.automation.models import AgentRun
from apps.automation.shadow import run_shadow
from apps.core.tasks import PhasedTenantTask, PlatformTask
from apps.core.tenancy import tenant_context


@shared_task(base=PhasedTenantTask, soft_time_limit=25, time_limit=30)
def classify_document(tenant_id: str, intake_id: str) -> str:
    """All database phases are scoped; provider I/O is outside transactions."""
    return run_shadow(UUID(tenant_id), UUID(intake_id))


def enqueue_shadow(tenant_id: UUID, intake_id: UUID) -> bool:
    """Broker unavailability cannot roll back or fail deterministic extraction."""
    if not eligible(tenant_id, intake_id):
        return False
    try:
        classify_document.delay(str(tenant_id), str(intake_id))
    except OperationalError as error:
        sentry_sdk.capture_exception(error)
        return False
    return True


@shared_task(base=PlatformTask, bind=True)
def purge_agent_outputs(self: PlatformTask) -> int:
    """Expire content, never budget receipts, including firms since disabled.

    Retention is not feature rollout: removing ai_shadow cannot retain old text.
    Unflagged firms without prior runs have no rows and no user-visible effects.
    """
    changed = 0
    for tid in self.tenant_ids(include_inactive=True):
        with tenant_context(tid):
            changed += (
                AgentRun.objects.filter(expires_at__lte=timezone.now())
                .exclude(output={})
                .update(output={})
            )
    return changed

"""Provider work queued by staff actions; each phase owns its tenant context."""

from uuid import UUID

from celery import Task, shared_task
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.core.tasks import PhasedTenantTask
from apps.core.tenancy import tenant_context
from apps.integrations.call_models import ProviderCall
from apps.integrations.serpro.pgmei import (
    GuideRefused,
    obtain_das_guide,
    settle_guide,
)

# A retry waits for the in-flight attempt's lease to end, then settles that call.
RETRY_MARGIN_SECONDS = 5
MAX_SETTLE_RETRIES = 10


def _retry_in_seconds(tenant_id: UUID, call_id: UUID) -> int:
    with tenant_context(tenant_id):
        lease = (
            ProviderCall.objects.filter(pk=call_id)
            .values_list("lease_until", flat=True)
            .first()
        )
    remaining = (lease - timezone.now()).total_seconds() if lease else 0
    return max(int(remaining), 0) + RETRY_MARGIN_SECONDS


@shared_task(base=PhasedTenantTask, bind=True, max_retries=MAX_SETTLE_RETRIES)
def obtain_das_guide_task(
    self: Task,
    tenant_id: str,
    actor_id: str,
    cycle_id: str,
    call_id: str | None = None,
) -> str:
    """Obtain one staff-requested official DAS guide (AXRM-017); never scheduled.

    Authority, eligibility and the daily idempotency key are re-checked here, at
    send time, not trusted from the click that queued the task. A run that finds
    the attempt still in flight (an acks-late redelivery inside the dead worker's
    lease, or a concurrent click) is never acknowledged as done: it retries with
    that call's id and then only settles that call - owning it once the lease has
    run out - so a retry after midnight never emits under a new day's key.
    """
    tenant, cycle = UUID(tenant_id), UUID(cycle_id)
    actor = User.objects.get(pk=UUID(actor_id))
    try:
        if call_id is not None:
            target = UUID(call_id)
            code = settle_guide(tenant, target, cycle_id=cycle, actor=actor)
        else:
            outcome = obtain_das_guide(actor, tenant, cycle)
            target, code = outcome.call_id, outcome.code
    except GuideRefused as refused:
        return refused.code
    except (PermissionDenied, Http404):
        return "acesso_negado"
    if code == "em_andamento":
        raise self.retry(
            args=(tenant_id, actor_id, cycle_id),
            kwargs={"call_id": str(target)},
            countdown=_retry_in_seconds(tenant, target),
        )
    return code

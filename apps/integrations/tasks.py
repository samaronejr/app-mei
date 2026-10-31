"""Provider work queued by staff actions; each phase owns its tenant context."""

from uuid import UUID

from celery import shared_task
from django.core.exceptions import PermissionDenied
from django.http import Http404

from apps.accounts.models import User
from apps.core.tasks import PhasedTenantTask
from apps.integrations.serpro.pgmei import GuideRefused, obtain_das_guide


@shared_task(base=PhasedTenantTask)
def obtain_das_guide_task(tenant_id: str, actor_id: str, cycle_id: str) -> str:
    """Obtain one staff-requested official DAS guide (AXRM-017); never scheduled.

    Authority, eligibility and the daily idempotency key are re-checked here, at
    send time, not trusted from the click that queued the task.
    """
    actor = User.objects.get(pk=UUID(actor_id))
    try:
        return obtain_das_guide(actor, UUID(tenant_id), UUID(cycle_id)).code
    except GuideRefused as refused:
        return refused.code
    except (PermissionDenied, Http404):
        return "acesso_negado"

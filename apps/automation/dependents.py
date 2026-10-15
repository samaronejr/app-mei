"""Private processing receipts prevent deletion through import rollback."""

from apps.automation.models import AgentRun
from apps.clients.importing import register_rollback_dependent

register_rollback_dependent(
    "automation.agentrun",
    "registros internos de processamento",
    lambda tenant_id, ids: AgentRun.objects.filter(
        tenant_id=tenant_id, client_id__in=ids
    ),
)

"""Service cycles protect imported clients from destructive import rollback."""

from apps.clients.importing import register_rollback_dependent
from apps.cycles.models import ServiceCycle

register_rollback_dependent(
    "cycles.servicecycle",
    "ciclos de serviço",
    lambda tenant_id, ids: ServiceCycle.objects.filter(
        tenant_id=tenant_id, client_id__in=ids
    ),
)

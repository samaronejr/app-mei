"""Operational endpoints that belong to no tenant."""

from django.db import transaction
from django.http import HttpRequest, JsonResponse


# ATOMIC_REQUESTS wraps every view in a transaction, which would make this probe open a
# database connection. A liveness probe must not: the orchestrator uses it to decide
# whether to restart the web process, and a database blip must not restart a healthy
# process. Readiness (which does check dependencies) is a separate, later endpoint.
@transaction.non_atomic_requests
def healthz(_request: HttpRequest) -> JsonResponse:
    """Return 200 and a minimal JSON body for container and load-balancer probes."""
    return JsonResponse({"status": "ok"})

"""Operational endpoints that belong to no tenant."""

from django.db import DatabaseError, transaction
from django.http import HttpRequest, JsonResponse

from apps.obligations.heartbeat import scheduler_is_alive

SERVICE_UNAVAILABLE = 503


# ATOMIC_REQUESTS wraps every view in a transaction, which this probe must not open:
# an orchestrator uses it to decide whether to restart the web process, and a
# transaction here would tie that decision to the database's health.
#
# The scheduler check below IS a database read, which looks like a contradiction and
# is not. It runs in autocommit, and a database failure is caught and reported as
# `unknown` rather than propagating — so the probe still answers 200 when Postgres is
# down. That is the correct liveness answer: restarting the web process does not fix a
# database outage, it only removes the last component still able to serve anything.
# Readiness, which SHOULD fail on a dependency outage, is a separate later endpoint.
@transaction.non_atomic_requests
def healthz(_request: HttpRequest) -> JsonResponse:
    """Report process liveness, and whether the scheduler has gone quiet.

    The 503 exists because the scheduler's failure mode is silence: when beat stops,
    no task raises and nothing reaches Sentry, so the only way to notice is to look
    for the missing signal.
    """
    try:
        alive = scheduler_is_alive()
    except DatabaseError:
        # The exact exception, at the exact boundary. Anything broader would swallow
        # a programming error in scheduler_is_alive and report a healthy process.
        return JsonResponse({"status": "ok", "scheduler": "unknown"})

    if alive:
        return JsonResponse({"status": "ok", "scheduler": "alive"})
    return JsonResponse(
        {"status": "degraded", "scheduler": "stale"},
        status=SERVICE_UNAVAILABLE,
    )

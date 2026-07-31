"""Operational endpoints that belong to no tenant."""

from django.db import DatabaseError, transaction
from django.http import HttpRequest, JsonResponse

from apps.obligations.heartbeat import read_heartbeats

OK = 200
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
    """Report process liveness, scheduler silence, and backup staleness.

    Both signals exist because their failure mode is silence: when beat stops or the
    nightly backup starts failing, no task raises and nothing reaches Sentry, so the
    only way to notice is to look for the missing signal.

    **Only the scheduler answers 503.** A dead beat means the application is not doing
    its job and restarting the web process is a reasonable response. A stale backup
    means the application is fine and a human must act — so it is reported in the body
    and nowhere else. Escalating it to 503 would flap the container healthcheck, fail
    the deploy job's fourth assertion, and take the site down over a problem the site
    does not have: a worse outage than the one being reported.
    """
    try:
        report = read_heartbeats()
    except DatabaseError:
        # The exact exception, at the exact boundary. Anything broader would swallow
        # a programming error in read_heartbeats and report a healthy process.
        return JsonResponse(
            {"status": "ok", "scheduler": "unknown", "backup": "unknown"},
        )

    return JsonResponse(
        {
            "status": "ok" if report.scheduler_alive else "degraded",
            "scheduler": "alive" if report.scheduler_alive else "stale",
            "backup": "fresh" if report.backup_fresh else "stale",
        },
        status=OK if report.scheduler_alive else SERVICE_UNAVAILABLE,
    )

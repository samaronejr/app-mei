"""Operational endpoints that belong to no tenant."""

from django.core.cache import InvalidCacheBackendError, caches
from django.db import DatabaseError, connections, transaction
from django.http import HttpRequest, JsonResponse
from redis.exceptions import RedisError

from apps.obligations.heartbeat import DiskHeadroom, read_heartbeats

OK = 200
SERVICE_UNAVAILABLE = 503
READY_CACHE_KEY = "readyz:probe"
READY_CACHE_VALUE = "ok"
READY_CACHE_TIMEOUT_SECONDS = 5


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
    """Report process liveness, scheduler silence, backup staleness, and disk headroom.

    All three signals exist because their failure mode is silence: when beat stops, when
    the nightly backup starts failing, or when the WAL archive eats the disk, no task
    raises and nothing reaches Sentry, so the only way to notice is to look for the
    missing signal.

    **Only the scheduler answers 503.** A dead beat means the application is not doing
    its job and restarting the web process is a reasonable response. A stale backup or a
    filling disk means the application is fine and a human must act — so they are
    reported in the body and nowhere else. Escalating either to 503 would flap the
    container healthcheck, fail the deploy job's fourth assertion, and take the site
    down over a problem the site does not have: a worse outage than the one reported.
    """
    try:
        report = read_heartbeats()
    except DatabaseError:
        # The exact exception, at the exact boundary. Anything broader would swallow
        # a programming error in read_heartbeats and report a healthy process.
        return JsonResponse(
            {
                "status": "ok",
                "scheduler": "unknown",
                "backup": "unknown",
                "disk": DiskHeadroom.UNKNOWN.value,
            },
        )

    return JsonResponse(
        {
            "status": "ok" if report.scheduler_alive else "degraded",
            "scheduler": "alive" if report.scheduler_alive else "stale",
            "backup": "fresh" if report.backup_fresh else "stale",
            "disk": report.disk.value,
        },
        status=OK if report.scheduler_alive else SERVICE_UNAVAILABLE,
    )


@transaction.non_atomic_requests
def readyz(_request: HttpRequest) -> JsonResponse:
    """Report whether the database and Redis can serve application traffic."""
    database_status = "ok"
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        database_status = "error"

    redis_status = "ok"
    try:
        cache = caches["default"]
        cache.set(
            READY_CACHE_KEY,
            READY_CACHE_VALUE,
            timeout=READY_CACHE_TIMEOUT_SECONDS,
        )
        if cache.get(READY_CACHE_KEY) != READY_CACHE_VALUE:
            redis_status = "error"
    except (InvalidCacheBackendError, RedisError):
        redis_status = "error"
    ready = database_status == redis_status == "ok"
    return JsonResponse(
        {
            "status": "ready" if ready else "unready",
            "database": database_status,
            "redis": redis_status,
        },
        status=OK if ready else SERVICE_UNAVAILABLE,
    )

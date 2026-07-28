"""Scheduled work for the obligation engine.

Both tasks run outside the request cycle, so `TenantMiddleware` never touches them
and `app.tenant_id` is never set. Under the fail-closed policy that would make a
sweep see zero rows and report success — a nightly job that silently does nothing.
`PlatformTask.each_tenant()` enters and leaves one firm's context per step, so no
query ever runs with two tenants in scope and none runs with no tenant at all.
"""

from celery import shared_task
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.core.tasks import PlatformTask
from apps.obligations.generator import generate_das_calendar
from apps.obligations.heartbeat import record_heartbeat


@shared_task
def record_scheduler_heartbeat() -> str:
    """Assert that beat is still alive, so its silence becomes detectable.

    Deliberately trivial and dependency-free. Any work here would give the heartbeat
    a way to fail for reasons unrelated to the scheduler being up, and a liveness
    signal that can fail spuriously teaches everyone to ignore it.
    """
    record_heartbeat()
    return "ok"


@shared_task(base=PlatformTask, bind=True)
def refresh_das_calendars(self: PlatformTask) -> int:
    """Extend every active client's rolling DAS window, one tenant at a time.

    Idempotent by construction: the generator's insert is `ON CONFLICT DO NOTHING`
    against a unique constraint, so a redelivered run adds only the months that have
    newly entered the window.
    """
    today = timezone.localdate()
    created = 0
    for _tenant in self.each_tenant():
        for client in ClientCompany.objects.filter(is_mei=True):
            created += generate_das_calendar(client, starting_from=today)
    return created

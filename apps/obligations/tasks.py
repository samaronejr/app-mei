"""Scheduled work for the obligation engine.

Both tasks run outside the request cycle, so `TenantMiddleware` never touches them
and `app.tenant_id` is never set. Under the fail-closed policy that would make a
sweep see zero rows and report success — a nightly job that silently does nothing.
`PlatformTask.each_tenant()` enters and leaves one firm's context per step, so no
query ever runs with two tenants in scope and none runs with no tenant at all.
"""

from celery import shared_task
from django.utils import timezone

from apps.clients.models import ClientCompany, TaxRegime
from apps.clients.services import claim_at, current_profiles
from apps.core.tasks import PlatformTask
from apps.obligations.generator import first_competence_for, generate_das_calendar
from apps.obligations.heartbeat import record_heartbeat
from apps.obligations.rules import DueRuleCache


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

    **One `DueRuleCache` for the whole sweep, built outside both loops.** Constructed
    per client it would cost one rule read per client, which is the N+1 shape the
    queue budgets forbid — and it would be invisible, because the calendar would still
    be correct. Sharing it across `each_tenant()` is safe precisely because the eras
    are platform reference data: `obligations_obligationduerule` carries no tenant
    column and no policy, so there is no per-firm answer for a cache to leak.

    **MEI is asked of the tax profile, not the legacy flag.** `is_mei` can no longer
    be updated retroactively — it is a boolean, and a client who was MEI in January
    and Simples by April needs the answer per competence month, which only the
    effective-dated timeline can give. The sweep therefore asks for the regime at
    the client's first competence in the window (`first_competence_for`), fetched
    for the whole portfolio in one query rather than one per client. A client with
    no claim at all falls back to the legacy flag: that is what the flag asserted
    about rows the backfill predates or importers wrote, and skipping them would
    silently stop generating deadlines for clients the firm believes are MEI.
    """
    today = timezone.localdate()
    rules = DueRuleCache()
    created = 0
    for _tenant in self.each_tenant():
        clients = list(ClientCompany.objects.all())
        profiles = current_profiles(clients)
        for client in clients:
            competence = first_competence_for(client, today)
            timeline = profiles.get(client.pk, [])
            if timeline:
                # The timeline has spoken: a gap is UNKNOWN, and unknown is not MEI.
                # Falling back to the flag here would resurrect a regime the firm's
                # own corrections deliberately closed.
                claim = claim_at(timeline, competence)
                is_mei = claim is not None and claim.regime == TaxRegime.MEI
            else:
                # No claim was ever recorded — rows the backfill predates or an
                # importer wrote. The legacy flag is the only assertion they carry.
                is_mei = client.is_mei
            if is_mei:
                created += generate_das_calendar(
                    client,
                    starting_from=today,
                    rules=rules,
                )
    return created

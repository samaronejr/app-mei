"""Monthly generation: one tenant transaction, replay-safe natural keys."""

from datetime import date
from uuid import UUID

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from apps.clients.models import Engagement
from apps.core.flags import tenant_has
from apps.core.tasks import PlatformTask, TenantTask
from apps.cycles.models import PlaybookVersion, VersionState
from apps.cycles.services.cycle import open_cycle
from apps.cycles.targeting import AppliesTo, matching_clients
from apps.obligations.models import SchedulerHeartbeat
from apps.tenants.models import Tenant


@shared_task(base=TenantTask)
def generate_cycles_for_period(tenant_id: UUID | str, period: str) -> dict[str, int]:
    """Return accurate inserted/existing counts; an unflagged firm is untouched."""
    tenant_pk = UUID(str(tenant_id))
    counts = {"created": 0, "existing": 0}
    if not tenant_has(tenant_pk, "cycles_r1"):
        return counts
    on = date.fromisoformat(period)
    if on.day != 1:
        msg = "period must start on day 1"
        raise ValueError(msg)
    # PLATFORM_QUERY_OK: the TenantTask context names this exact firm; generation
    # never enumerates tenant data outside that explicit context.
    tenant = Tenant.objects.get(pk=tenant_pk)
    definitions = (
        PlaybookVersion.objects.filter(state=VersionState.PUBLISHED)
        .order_by("playbook_id", "-number")
        .distinct("playbook_id")
    )
    for definition in definitions:
        scope = AppliesTo.parse(definition.applies_to_json)
        clients = matching_clients(tenant, definition.applies_to_json, on)
        engagements = Engagement.objects.filter(
            client_id__in=clients.values("pk"), starts_on__lte=on
        ).filter(Q(ends_on__isnull=True) | Q(ends_on__gt=on))
        if scope.service_kinds:
            engagements = engagements.filter(service_kind__in=scope.service_kinds)
        for engagement in engagements.order_by("pk"):
            ref = open_cycle(
                None, engagement.pk, on, definition.pk, idempotency_key=f"period:{on}"
            )
            counts["created" if ref.created else "existing"] += 1
    return counts


@shared_task(base=PlatformTask, bind=True)
def generate_monthly_cycles(self: PlatformTask) -> dict[str, int]:
    """Beat calls the TenantTask only for enabled tenants, then stamps success.

    This is database-only work: synchronous task calls preserve failure reporting.
    A failed tenant prevents the global heartbeat from falsely claiming success.
    """
    counts = {"created": 0, "existing": 0}
    period = timezone.localdate().replace(day=1).isoformat()
    for tenant in self.each_tenant():
        if tenant_has(tenant.pk, "cycles_r1"):
            result = generate_cycles_for_period(str(tenant.pk), period)
            for key in counts:
                counts[key] += result[key]
    SchedulerHeartbeat.objects.update_or_create(name="generate_cycles", defaults={})
    return counts

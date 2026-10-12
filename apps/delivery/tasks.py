"""Minute dispatch: collect tenants, commit claims, send, fenced finalization."""

from collections.abc import Callable
from uuid import UUID

from celery import shared_task

from apps.core.tasks import PhasedTenantTask, PlatformTask, assert_no_transaction
from apps.delivery.gateway import send
from apps.delivery.policy import enabled_kinds
from apps.delivery.services import claim_batch, finalize
from apps.obligations.models import SchedulerHeartbeat


@shared_task(base=PhasedTenantTask)
def dispatch_tenant(
    tenant_id: UUID | str, *, before_network_io: Callable[[], None] | None = None
) -> int:
    """No outer transaction; injected hook observes the actual egress boundary."""
    claims = claim_batch(UUID(str(tenant_id)))
    for claim in claims:
        reply = send(claim, before_network_io=before_network_io)
        finalize(claim, reply)
    return len(claims)


@shared_task(base=PlatformTask, bind=True)
def dispatch_outbox(self: PlatformTask) -> int:
    """Propagate failed phases; never advertise partial success with a heartbeat."""
    assert_no_transaction()
    count = 0
    for tid in self.tenant_ids():
        if enabled_kinds(tid):
            count += dispatch_tenant(str(tid))
    SchedulerHeartbeat.objects.update_or_create(name="dispatch_outbox", defaults={})
    return count

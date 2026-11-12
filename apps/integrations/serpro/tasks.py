"""Worker entry points for PAGTOWEB; no provider I/O ever runs in a web request."""

from datetime import date, timedelta
from typing import Final
from uuid import UUID

from celery import shared_task
from django.utils import timezone
from django.utils.timezone import now as wall_clock

from apps.core.tasks import PhasedTenantTask, PlatformTask
from apps.integrations.serpro import sweeps
from apps.integrations.serpro.models import ACTIVE_QUERY_STATES
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.integrations.serpro.payments import (
    emit_payment_receipt,
    purge_kept_pages,
    run_payment_query,
)

SCHEDULE_AFTER_DAY: Final = 20
# How long after the owner's lease deadline a yielded delivery looks again.
RESUME_MARGIN: Final = timedelta(seconds=30)


@shared_task(base=PhasedTenantTask)
def run_payment_query_task(tenant_id: str, query_id: str) -> str:
    """Run or resume one consultation; a redelivered message never rebills a page.

    A delivery that yielded to the runner holding the settlement lease is
    acknowledged like any other, so it schedules this task again for just after
    that lease ends: if the owner died, the consultation resumes without staff or
    another delivery; if it is still working, the continuation yields and looks
    again. It ends once the consultation is final.
    """
    query = run_payment_query(UUID(tenant_id), UUID(query_id))
    if query.state in ACTIVE_QUERY_STATES:
        # Real time, whatever calendar day the schedule believes it is.
        now = wall_clock()
        deadline = query.settlement_until or now
        # Nobody waits for the continuation: no result is subscribed to or kept.
        run_payment_query_task.apply_async(
            (tenant_id, query_id),
            eta=max(deadline, now) + RESUME_MARGIN,
            ignore_result=True,
        )
    return query.state


@shared_task(base=PlatformTask, bind=True)
def purge_payment_pages(self: PlatformTask) -> int:
    """Daily retention for kept PAGTOWEB pages, one firm at a time.

    Inactive firms are visited too: retention does not wait for a firm to return.
    """
    return sum(
        purge_kept_pages() for _tenant in self.each_tenant(include_inactive=True)
    )


@shared_task(base=PhasedTenantTask)
def emit_payment_receipt_task(tenant_id: str, payment_id: str, actor_id: str) -> str:
    """Issue one payment's receipt; the payment's call identity makes it idempotent."""
    call = emit_payment_receipt(UUID(tenant_id), UUID(payment_id), UUID(actor_id))
    return call.state if call is not None else "recusada"


def scheduled_window(today: date) -> PaymentRange | None:
    """After the 20th, the previous competence month up to today (A6 SP-PAG)."""
    if today.day <= SCHEDULE_AFTER_DAY:
        return None
    competence = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    return PaymentRange(competence, today)


@shared_task(base=PlatformTask, bind=True)
def schedule_payment_consultations(self: PlatformTask) -> int:
    """Open one system consultation per linked client and month, then enqueue it.

    Beat fires daily from 06:20; the consultation's identity is the client and the
    competence month, never the day, so once the month's consultation exists later
    days return it and nothing new is opened or billed (A6 SP-PAG). Each firm gets
    a bounded, cursor-ordered turn and every guard is checked first (AXRM-018).
    """
    window = scheduled_window(timezone.localdate())
    if window is None:
        return 0
    return sweeps.sweep_payments(self, window)


@shared_task(base=PlatformTask, bind=True)
def schedule_situation_reports(self: PlatformTask) -> int:
    """Request this month's SITFIS report for each client whose cycle opened.

    Beat fires daily from 06:10; a client that already has a report this month, in
    any state, is not requested again (A6 SP-SIT). The report's own worker task
    drives the protocol, the wait and the emission.
    """
    return sweeps.sweep_reports(self, timezone.localdate())

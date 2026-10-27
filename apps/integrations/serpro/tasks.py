"""Worker entry points for PAGTOWEB; no provider I/O ever runs in a web request."""

from datetime import date, timedelta
from typing import Final
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.flags import tenant_has
from apps.core.tasks import PhasedTenantTask, PlatformTask, assert_no_transaction
from apps.integrations.models import ClientProviderLink, LinkStatus
from apps.integrations.serpro.models import PaymentQueryState
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.integrations.serpro.payments import (
    PaymentQueryConflictError,
    emit_payment_receipt,
    open_payment_query,
    run_payment_query,
)

SCHEDULE_AFTER_DAY: Final = 20


@shared_task(base=PhasedTenantTask)
def run_payment_query_task(tenant_id: str, query_id: str) -> str:
    """Run or resume one consultation; a redelivered message never rebills a page."""
    return run_payment_query(UUID(tenant_id), UUID(query_id)).state


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
    """Open one system consultation per linked client and month, then enqueue it."""
    assert_no_transaction()
    today = timezone.localdate()
    window = scheduled_window(today)
    if window is None:
        return 0
    queued: list[tuple[str, str]] = []
    for tenant in self.each_tenant():
        if not tenant_has(tenant.pk, "federal_checks"):
            continue
        links = (
            ClientProviderLink.objects.filter(
                connection__provider="serpro",
                connection__environment=settings.SERPRO_ENVIRONMENT,
                status=LinkStatus.ATIVA,
            )
            .select_related("client")
            .order_by("pk")
        )
        for link in links:
            try:
                query = open_payment_query(
                    link.client, window, actor=None, nonce=f"agendada:{today:%Y-%m}"
                )
            except PaymentQueryConflictError:
                continue
            if query.state == PaymentQueryState.NA_FILA:
                queued.append((str(query.tenant_id), str(query.pk)))
    for tenant_id, query_id in queued:
        run_payment_query_task.delay(tenant_id, query_id)
    return len(queued)

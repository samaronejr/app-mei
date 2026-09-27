"""Reference-only outbox projection for the authorized client workspace."""

from datetime import date
from uuid import UUID

from django.db.models import Q

from apps.cycles.rules import next_period_start
from apps.cycles.workspace import TimelineEntry
from apps.delivery.models import DeliveryAttempt, MessageState


def read_timeline(
    tenant_id: UUID, client_id: UUID, period: date
) -> list[TimelineEntry]:
    """Merge SMTP observations without message bodies or recipient addresses."""
    attempts = DeliveryAttempt.objects.filter(
        Q(message__cycle__period_start=period)
        | Q(
            message__cycle__isnull=True,
            occurred_at__date__gte=period,
            occurred_at__date__lt=next_period_start(period),
        ),
        tenant_id=tenant_id,
        client_id=client_id,
    ).order_by("-occurred_at", "-pk")[:100]
    labels = dict(MessageState.choices)
    return [
        TimelineEntry(
            row.pk,
            row.occurred_at,
            labels.get(row.outcome, "Observação de envio"),
            "Comunicação",
            str(row.message_id),
        )
        for row in attempts
    ]

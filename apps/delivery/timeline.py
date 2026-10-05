"""Reference-only outbox projection for the authorized client workspace."""

from datetime import date
from uuid import UUID

from django.db.models import Q

from apps.cycles.models import ClientPublication
from apps.cycles.rules import next_period_start
from apps.cycles.workspace import TimelineEntry
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage


def message_history(messages: list[OutboundMessage]) -> list[dict[str, object]]:
    """Project one already-authorized page, separating SMTP and client receipts."""
    attempts: dict[UUID, list[dict[str, object]]] = {}
    labels = dict(MessageState.choices)
    for attempt in DeliveryAttempt.objects.filter(
        message_id__in=[row.pk for row in messages]
    ).order_by("occurred_at", "pk"):
        attempts.setdefault(attempt.message_id, []).append(
            {
                "label": labels.get(attempt.outcome, "Observação do envio"),
                "when": attempt.occurred_at,
                "code": attempt.error_code,
            }
        )
    publications = {
        row.proposal_id: row
        for row in ClientPublication.objects.filter(
            proposal_id__in=[row.proposal_id for row in messages]
        ).prefetch_related("acknowledgements__user")
    }
    return [
        {
            "message": row,
            "timeline": attempts.get(row.pk, []),
            "publication": (
                publications.get(row.proposal_id)
                if row.proposal_id is not None
                else None
            ),
        }
        for row in messages
    ]


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

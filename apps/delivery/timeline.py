"""Reference-only outbox projection for the authorized client workspace."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from django.db.models import Q

from apps.cycles.models import ClientPublication
from apps.cycles.rules import next_period_start
from apps.cycles.workspace import TimelineEntry
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage


class ObservationEffect(StrEnum):
    """What one append-only receipt did to its message when it was recorded.

    `services.transition` records every reply but applies only an allowed
    transition, and `services.finalize` records a superseded fence's reply
    without touching the message. Both rows keep the outcome they observed, so
    the projection must say that the row did not move the message's state.
    """

    APPLIED = "applied"
    IGNORED_LATE = "ignored_late"
    IGNORED_STALE = "ignored_stale"


# The codes apps.delivery.services writes on a receipt it did not apply. Read only:
# a stored receipt is never re-coded, rewritten or backfilled.
IGNORED_CODES: Final[dict[str, ObservationEffect]] = {
    "late_observation": ObservationEffect.IGNORED_LATE,
    "stale_fence": ObservationEffect.IGNORED_STALE,
}
# Choices labels are typed as possibly lazy; these are plain pt-BR strings.
STATE_LABELS: Final[dict[str, str]] = {
    value: str(label) for value, label in MessageState.choices
}
IGNORED_DETAILS: Final[dict[ObservationEffect, str]] = {
    ObservationEffect.IGNORED_LATE: (
        "Registrada só como histórico. Não alterou o estado da mensagem."
    ),
    ObservationEffect.IGNORED_STALE: (
        "Veio de uma tentativa já substituída e ficou só no histórico. "
        "Não alterou o estado da mensagem."
    ),
}


@dataclass(frozen=True, slots=True)
class DeliveryObservation:
    """One receipt as recorded, typed by whether it changed its message.

    `observed_state` is the outcome the receipt reports, never the message's
    current state; an ignored receipt keeps its own outcome, code and time.
    """

    when: datetime
    observed_state: str
    observed_label: str
    code: str
    effect: ObservationEffect

    @property
    def ignored(self) -> bool:
        """Return whether the receipt was recorded without changing the message."""
        return self.effect is not ObservationEffect.APPLIED

    @property
    def summary(self) -> str:
        """Name the observed outcome, qualified when it had no effect."""
        if self.ignored:
            return f"Observação ignorada: {self.observed_label}"
        return self.observed_label

    @property
    def detail(self) -> str:
        """Explain why an ignored receipt had no effect; empty when applied."""
        return IGNORED_DETAILS.get(self.effect, "")


def observe(attempt: DeliveryAttempt, fallback: str) -> DeliveryObservation:
    """Project one receipt without reading or changing its message."""
    return DeliveryObservation(
        when=attempt.occurred_at,
        observed_state=attempt.outcome,
        observed_label=STATE_LABELS.get(attempt.outcome, fallback),
        code=attempt.error_code,
        effect=IGNORED_CODES.get(attempt.error_code, ObservationEffect.APPLIED),
    )


def message_history(
    messages: list[OutboundMessage], *, redact: bool = False
) -> list[dict[str, object]]:
    """Project one already-authorized page, separating SMTP and client receipts.

    `redact` is the support-session rendering: the queue still proves the
    timeline happened — states, attempts, codes — while the subject and the
    staff note, which are free text that can carry client details, are replaced
    rather than shown. The row itself is untouched; only the projection is.
    """
    attempts: dict[UUID, list[DeliveryObservation]] = {}
    for attempt in DeliveryAttempt.objects.filter(
        message_id__in=[row.pk for row in messages]
    ).order_by("occurred_at", "pk"):
        attempts.setdefault(attempt.message_id, []).append(
            observe(attempt, "Observação do envio")
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
            "display_subject": "•••" if redact else row.subject,
            "display_note": (
                "•••" if redact and row.decision_note else row.decision_note
            ),
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
    observed = [(row, observe(row, "Observação de envio")) for row in attempts]
    return [
        TimelineEntry(
            row.pk,
            row.occurred_at,
            observation.summary,
            "Comunicação",
            str(row.message_id),
            ignored=observation.ignored,
            detail=observation.detail,
        )
        for row, observation in observed
    ]

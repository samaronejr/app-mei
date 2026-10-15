"""Reference-only outbox projection for the authorized client workspace."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from django.db.models import Prefetch, Q

from apps.cycles.models import ClientAcknowledgement, ClientPublication
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
# What a support session sees instead of free text that can carry client details.
REDACTED: Final = "•••"
# How many of a competence's messages the workspace lists before it points at the
# queue. One more row is read to know whether there are others (AXR1-043).
PERIOD_MESSAGE_LIMIT: Final = 20
# Every kind a producer writes, in pt-BR. A reminder can come from a person or from
# the T39 sweep, so its word names what it is about, never who queued it.
MESSAGE_KIND_LABELS: Final[dict[str, str]] = {
    "notice": "Aviso por e-mail",
    "portal_invite": "Convite ao portal",
    "contact_portal_invite": "Convite ao portal",
    "request_notice": "Aviso de solicitação de documentos",
    "request_reminder": "Lembrete de documento pendente",
    "approved_email": "E-mail aprovado no ciclo",
    "approved_publication_notice": "Aviso de publicação no portal",
}
UNKNOWN_KIND_LABEL: Final = "Outro tipo de mensagem"


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


@dataclass(frozen=True, slots=True)
class PeriodMessage:
    """One message of a competence: its one current state and its receipts.

    `message.state` is the only current state. `observations` is the recorded
    history in order; an ignored receipt keeps its own outcome there and is never
    read as the state (AXR1-044). `subject` is already masked for support.
    """

    message: OutboundMessage
    kind_label: str
    subject: str
    observations: tuple[DeliveryObservation, ...]


@dataclass(frozen=True, slots=True)
class PeriodPublication:
    """One portal publication of a competence and the client's explicit receipts."""

    publication: ClientPublication
    title: str
    acknowledgements: tuple[ClientAcknowledgement, ...]

    @property
    def acknowledged(self) -> bool:
        """Return whether a client recorded receipt of this exact published version."""
        digest = self.publication.publication_digest
        return any(row.publication_digest == digest for row in self.acknowledgements)


@dataclass(frozen=True, slots=True)
class PeriodCommunication:
    """The workspace's Comunicação panel for one authorized client and competence."""

    messages: tuple[PeriodMessage, ...]
    more_messages: bool
    publications: tuple[PeriodPublication, ...]


def _observations(
    messages: list[OutboundMessage],
) -> dict[UUID, list[DeliveryObservation]]:
    """Type every receipt of one page of messages, in recorded order, in one query."""
    grouped: dict[UUID, list[DeliveryObservation]] = {}
    for attempt in DeliveryAttempt.objects.filter(
        message_id__in=[row.pk for row in messages]
    ).order_by("occurred_at", "pk"):
        grouped.setdefault(attempt.message_id, []).append(
            observe(attempt, "Observação do envio")
        )
    return grouped


def message_history(
    messages: list[OutboundMessage], *, redact: bool = False
) -> list[dict[str, object]]:
    """Project one already-authorized page, separating SMTP and client receipts.

    `redact` is the support-session rendering: the queue still proves the
    timeline happened — states, attempts, codes — while the subject and the
    staff note, which are free text that can carry client details, are replaced
    rather than shown. The row itself is untouched; only the projection is.
    """
    attempts = _observations(messages)
    publications = {
        row.proposal_id: row
        for row in ClientPublication.objects.filter(
            proposal_id__in=[row.proposal_id for row in messages]
        ).prefetch_related("acknowledgements__user")
    }
    return [
        {
            "message": row,
            "display_subject": REDACTED if redact else row.subject,
            "display_note": (
                REDACTED if redact and row.decision_note else row.decision_note
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


def _period_messages(client_id: UUID, period: date) -> list[OutboundMessage]:
    """Return a competence's newest messages plus one, to tell whether more exist.

    A message belongs to its cycle's competence; one without a cycle (a portal
    invitation) belongs to the month it was created in, as in `read_timeline`.
    """
    return list(
        OutboundMessage.objects.filter(client_id=client_id)
        .filter(
            Q(cycle__period_start=period)
            | Q(
                cycle__isnull=True,
                created_at__date__gte=period,
                created_at__date__lt=next_period_start(period),
            )
        )
        .order_by("-created_at", "-pk")[: PERIOD_MESSAGE_LIMIT + 1]
    )


def _period_publications(client_id: UUID, period: date) -> list[ClientPublication]:
    """Return a competence's publications with every receipt and its account."""
    receipts = ClientAcknowledgement.objects.select_related("user").order_by(
        "acknowledged_at", "pk"
    )
    return list(
        ClientPublication.objects.filter(
            client_id=client_id, cycle__period_start=period
        )
        .prefetch_related(Prefetch("acknowledgements", queryset=receipts))
        .order_by("-published_at", "-pk")
    )


def period_communication(
    client_id: UUID, period: date, *, redact: bool = False
) -> PeriodCommunication:
    """Read the panel for a client the caller already resolved through its portfolio.

    At most four queries whatever the row count: messages, their receipts,
    publications, and their receipts joined to the receiving account. `redact` is
    the support-session rendering of `message_history`, applied to the same text.
    """
    rows = _period_messages(client_id, period)
    shown = rows[:PERIOD_MESSAGE_LIMIT]
    observations = _observations(shown)
    return PeriodCommunication(
        messages=tuple(
            PeriodMessage(
                message=row,
                kind_label=MESSAGE_KIND_LABELS.get(row.kind, UNKNOWN_KIND_LABEL),
                subject=REDACTED if redact else row.subject,
                observations=tuple(observations.get(row.pk, [])),
            )
            for row in shown
        ),
        more_messages=len(rows) > PERIOD_MESSAGE_LIMIT,
        publications=tuple(
            PeriodPublication(
                publication=row,
                title=REDACTED if redact else row.title,
                acknowledgements=tuple(row.acknowledgements.all()),
            )
            for row in _period_publications(client_id, period)
        ),
    )


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

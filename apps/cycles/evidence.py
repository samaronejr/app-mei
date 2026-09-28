"""Evidence requests and what the client sends against them (S6.3, S7.5, S8.2).

The split is the portal exposure rule made physical. `EvidenceRequest` holds only what
the client may read, because the portal role SELECTs the base table under client RLS;
anything firm-private about a request (instructions to the team, the waiver reason)
lives in `RequestInternal`, which the portal role holds no privilege on at all.

The client never UPDATEs anything. An upload INSERTs a `Document` (or reuses one with
the same bytes), an `EvidenceLink`, a `DocumentIntake` for a new document, and one
`CycleEvent`. "Enviado pelo cliente" is derived from the newest link, exactly as
"overdue" is derived from the due date; the stored `state` column moves only through
the locked, versioned firm-side services in `apps.cycles.services.requests`.
"""

from typing import ClassVar, Final

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.cycles.records import ClientRecord, ServiceCycle

TITLE_MAX_LENGTH: Final = 160


class RequestState(models.TextChoices):
    """Stored request states. `enviado_pelo_cliente` and overdue are derived."""

    DRAFT = "rascunho", "Rascunho"
    REQUESTED = "solicitado", "Solicitado"
    RECEIVED_BY_TEAM = "recebido_pela_equipe", "Recebido pela equipe"
    IN_REVIEW = "em_revisao", "Em revisão"
    ACCEPTED = "aceito", "Aceito"
    REJECTED = "rejeitado", "Rejeitado"
    WAIVED = "dispensado", "Dispensado"
    REPLACED = "substituido", "Substituído"


# States the client is still expected to act on. A rejected request goes back to the
# client (S6.3 `rejeitado -> solicitado`), so it is listed as open too.
CLIENT_OPEN_STATES: Final = frozenset({RequestState.REQUESTED, RequestState.REJECTED})
# States the client may see at all. Drafts were never sent; waived and replaced
# requests are the firm's bookkeeping.
CLIENT_VISIBLE_STATES: Final = frozenset(
    {
        RequestState.REQUESTED,
        RequestState.RECEIVED_BY_TEAM,
        RequestState.IN_REVIEW,
        RequestState.ACCEPTED,
        RequestState.REJECTED,
    }
)


class EvidenceRequest(ClientRecord):
    """One thing the firm asks the client for, inside one cycle.

    Client-visible by construction: the portal reads this table. Nothing firm-private
    may ever be added here -- put it on `RequestInternal`.
    """

    cycle = models.ForeignKey(
        ServiceCycle, on_delete=models.PROTECT, related_name="requests"
    )
    title = models.CharField(max_length=TITLE_MAX_LENGTH)
    client_note = models.TextField(blank=True)
    due_on = models.DateField(null=True, blank=True)
    required = models.BooleanField(default=True)
    state = models.CharField(
        max_length=24, choices=RequestState.choices, default=RequestState.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_evidence_requests",
    )
    requested_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Tenant-led index for the per-cycle list."""

        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "cycle", "state"], name="evidencerequest_cycle_idx"
            ),
        ]

    def __str__(self) -> str:
        """Identify the request by its client-facing title."""
        return self.title


class RequestInternal(ClientRecord):
    """Firm-only companion of a request: portal NONE (roles.sql, conftest)."""

    request = models.ForeignKey(
        EvidenceRequest, on_delete=models.PROTECT, related_name="internals"
    )
    instructions = models.TextField(blank=True)
    waiver_reason = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta(ClientRecord.Meta):
        """One companion per request; a bare OneToOne would omit client_id."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "request"],
                name="requestinternal_request_uniq",
            ),
        ]


class LinkOrigin(models.TextChoices):
    """Who attached the document to the request."""

    PORTAL = "portal", "Portal do cliente"
    STAFF = "staff", "Equipe"


class EvidenceLink(ClientRecord):
    """A document offered as evidence for one request. INSERT-only from the portal."""

    request = models.ForeignKey(
        EvidenceRequest, on_delete=models.PROTECT, related_name="links"
    )
    document = models.ForeignKey(
        "obligations.Document", on_delete=models.PROTECT, related_name="evidence_links"
    )
    origin = models.CharField(max_length=16, choices=LinkOrigin.choices)
    relinked = models.BooleanField(default=False)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="evidence_links",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """One link per request and document (S7.5)."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "request", "document"],
                name="evidencelink_request_document_uniq",
            ),
        ]


class IntakeState(models.TextChoices):
    """T11 writes `recebido` only; T12 adds quarantine and verification states."""

    RECEIVED = "recebido", "Recebido"


class DocumentIntake(ClientRecord):
    """The intake record of a newly stored document. INSERT-only from the portal."""

    document = models.ForeignKey(
        "obligations.Document", on_delete=models.PROTECT, related_name="intakes"
    )
    state = models.CharField(
        max_length=32, choices=IntakeState.choices, default=IntakeState.RECEIVED
    )
    reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """One intake per document (S6.2 DOCUMENT ||--|| DOCUMENTINTAKE)."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "document"],
                name="documentintake_document_uniq",
            ),
        ]

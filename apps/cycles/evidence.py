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
    """S6.2: `recebido -> quarentena -> (verificado | rejeitado_* | indisponivel)`.

    `recebido` is what T11 wrote before quarantine existed: bytes at the final key,
    never scanned. It is treated exactly like `quarentena` (pending a scan, served to
    nobody in the portal) and the scan sweep picks it up.
    """

    RECEIVED = "recebido", "Recebido"
    QUARANTINED = "quarentena", "Em verificação"
    VERIFIED = "verificado", "Verificado"
    REJECTED_MALWARE = "rejeitado_malware", "Recusado: ameaça detectada"
    REJECTED_TYPE = "rejeitado_tipo", "Recusado: tipo de arquivo não aceito"
    SCAN_UNAVAILABLE = "verificacao_indisponivel", "Verificação indisponível"


# Waiting for the scanner: the sweep claims these.
PENDING_SCAN_STATES: Final = frozenset({IntakeState.RECEIVED, IntakeState.QUARANTINED})
# Refused for good. The original is retained (never deleted) and never served, and a
# link to it does not count as the client having sent anything ("not linked").
REJECTED_STATES: Final = frozenset(
    {IntakeState.REJECTED_MALWARE, IntakeState.REJECTED_TYPE}
)


class ScanVerdict(models.TextChoices):
    """What the scanner said; empty until a scan (or the disabled switch) decides."""

    NONE = "", "Sem verificação"
    CLEAN = "limpo", "Limpo"
    INFECTED = "infectado", "Infectado"
    UNAVAILABLE = "indisponivel", "Indisponível"


def quarantine_key(tenant_id: object, client_id: object, storage_key: str) -> str:
    """Where a new upload waits for its verdict (S7.5; bucket prefix: OG-04)."""
    return f"quarantine/{tenant_id}/{client_id}/{storage_key}"


def counted_links() -> "models.QuerySet[EvidenceLink]":
    """Links that count as sent. A refused file (type or malware) is not linked (T12).

    Derived, never written: the portal cannot UPDATE a link, and a verdict that lands
    after the upload suspends the link by this filter alone.
    """
    return EvidenceLink.objects.exclude(document__intakes__state__in=REJECTED_STATES)


class DocumentIntake(ClientRecord):
    """The intake record of a newly stored document. INSERT-only from the portal.

    The portal INSERTs it (with the sniffed type and the quarantine key it wrote);
    only the firm-side scan service (`apps.cycles.scanning`) updates it, under a row
    lock and a `version` check, so a stale verdict can never overwrite a newer one.
    """

    document = models.ForeignKey(
        "obligations.Document", on_delete=models.PROTECT, related_name="intakes"
    )
    state = models.CharField(
        max_length=32, choices=IntakeState.choices, default=IntakeState.RECEIVED
    )
    reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # T12: type sniffing, the malware verdict and the retry schedule. Each NOT NULL
    # column also carries a database default, so an INSERT that names only the T11
    # columns (an older writer, a raw-SQL probe) still lands as a pending intake.
    sniffed_type = models.CharField(
        max_length=64, blank=True, default="", db_default=""
    )
    quarantine_key = models.CharField(
        max_length=255, blank=True, default="", db_default=""
    )
    verdict = models.CharField(
        max_length=16,
        choices=ScanVerdict.choices,
        blank=True,
        default=ScanVerdict.NONE,
        db_default=ScanVerdict.NONE,
    )
    scan_engine = models.CharField(max_length=64, blank=True, default="", db_default="")
    scan_signature_version = models.CharField(
        max_length=128, blank=True, default="", db_default=""
    )
    scanned_at = models.DateTimeField(null=True, blank=True)
    scan_attempts = models.PositiveIntegerField(default=0, db_default=0)
    next_scan_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1, db_default=1)

    @property
    def object_key(self) -> str:
        """Where the bytes are now: the quarantine key until the clean move."""
        if self.state == IntakeState.VERIFIED or not self.quarantine_key:
            return str(self.document.storage_key)
        return self.quarantine_key

    class Meta(ClientRecord.Meta):
        """One intake per document (S6.2 DOCUMENT ||--|| DOCUMENTINTAKE)."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "document"],
                name="documentintake_document_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            # The scan sweep: pending rows whose retry time has come, tenant-led.
            models.Index(
                fields=["tenant", "state", "next_scan_at"],
                name="documentintake_scan_due_idx",
            ),
        ]

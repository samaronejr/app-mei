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


class RequestKind(models.TextChoices):
    """What the request asks the client to send (T17).

    `comprovante` is a payment receipt: it must be bound to the obligation it
    pays for while the request is still a draft, so a submitted link can be
    attributed to a subject and period without parsing titles or filenames.
    Everything else stays the generic `documento` and carries no binding.
    """

    DOCUMENTO = "documento", "Documento"
    COMPROVANTE = "comprovante", "Comprovante de pagamento"


class EvidenceRequest(ClientRecord):
    """One thing the firm asks the client for, inside one cycle.

    Client-visible by construction: the portal reads this table. Nothing firm-private
    may ever be added here -- put it on `RequestInternal`.
    """

    cycle = models.ForeignKey(
        ServiceCycle, on_delete=models.PROTECT, related_name="requests"
    )
    # T17: receipt binding. A `comprovante` request names the obligation whose
    # payment it evidences (client-paired; the CHECK below keeps the pair
    # consistent). Identity fields are mutable only while `state == 'rascunho'`:
    # once sent, a wrong binding is fixed by a new correctly bound request, not
    # by rewriting one the client may have already answered.
    kind = models.CharField(
        "kind",
        max_length=16,
        choices=RequestKind.choices,
        default=RequestKind.DOCUMENTO,
    )
    obligation = models.ForeignKey(
        "obligations.Obligation",
        on_delete=models.PROTECT,
        related_name="evidence_requests",
        verbose_name="obligation",
        null=True,
        blank=True,
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
        """Tenant-led index for the per-cycle list, plus the receipt-binding pair."""

        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "cycle", "state"], name="evidencerequest_cycle_idx"
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            # A receipt always names its obligation; a generic document never may.
            # The mirror-image arms are deliberate: binding a `documento` request
            # would silently attach a submission to a payment it never claimed.
            models.CheckConstraint(
                condition=(
                    models.Q(kind="comprovante", obligation__isnull=False)
                    | models.Q(kind="documento", obligation__isnull=True)
                ),
                name="evidencerequest_kind_obligation_check",
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


class ReviewDecision(models.TextChoices):
    """What the firm decided about one submitted file (T13, S7.5)."""

    NONE = "", "Aguardando revisão"
    ACCEPTED = "aceito", "Aceito"
    REJECTED = "rejeitado", "Não aceito"


CLIENT_REASON_MAX_LENGTH: Final = 500


class EvidenceLink(ClientRecord):
    """A document offered as evidence for one request. INSERT-only from the portal.

    T13: `review_decision`, `client_reason` and `reviewed_at` are the client-visible
    projection of the firm's `Review` (which the portal never reads, S8.2). Only the
    firm-side review service writes them, under the request lock; the portal can
    INSERT a link but never UPDATE one, and a link it inserts with a forged decision
    only hides its own file from "sent" (`counted_links`) -- acceptance is the
    request's firm-written state, never this column.
    """

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
    review_decision = models.CharField(
        max_length=16,
        choices=ReviewDecision.choices,
        blank=True,
        default=ReviewDecision.NONE,
        db_default=ReviewDecision.NONE,
    )
    client_reason = models.TextField(blank=True, default="", db_default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1, db_default=1, editable=False)

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
    # T13: after a clean verdict the deterministic extraction classifies the file,
    # or finds it belongs to another company; an accepted review links it.
    CLASSIFIED = "classificado", "Lido, aguardando revisão"
    DIVERGENT = "divergente", "Divergente: documento de outra empresa"
    LINKED = "vinculado", "Vinculado"


# Waiting for the scanner: the sweep claims these.
PENDING_SCAN_STATES: Final = frozenset({IntakeState.RECEIVED, IntakeState.QUARANTINED})
# Refused for good. The original is retained (never deleted) and never served, and a
# link to it does not count as the client having sent anything ("not linked").
REJECTED_STATES: Final = frozenset(
    {IntakeState.REJECTED_MALWARE, IntakeState.REJECTED_TYPE}
)
# The scanner said clean: the bytes sit at the final key and the parsers may read
# them. Every state after `verificado` is still clean (T13).
CLEAN_STATES: Final = frozenset(
    {
        IntakeState.VERIFIED,
        IntakeState.CLASSIFIED,
        IntakeState.DIVERGENT,
        IntakeState.LINKED,
    }
)
# What the portal may re-serve. A document of another company (`divergente`) is
# clean but suspended: it goes back to nobody in the portal until a reviewer decides.
PORTAL_SERVABLE_STATES: Final = CLEAN_STATES - {IntakeState.DIVERGENT}
# A link to a document in these states does not count as the client having sent
# anything: refused files (T12) and documents of another company (T13).
SUSPENDED_STATES: Final = REJECTED_STATES | {IntakeState.DIVERGENT}
# A reviewer may decide on the file: extraction finished (or can never run, because
# the file was never scanned). `verificado` waits for extraction: deciding first would
# let a divergence found a second later go unseen.
REVIEWABLE_STATES: Final = frozenset(
    {
        IntakeState.CLASSIFIED,
        IntakeState.DIVERGENT,
        IntakeState.LINKED,
        IntakeState.SCAN_UNAVAILABLE,
    }
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
    after the upload suspends the link by this filter alone. T13 adds a document of
    another company (`divergente`) and a file the reviewer did not accept: the client
    is asked again, so neither counts as sent.
    """
    return EvidenceLink.objects.exclude(
        document__intakes__state__in=SUSPENDED_STATES
    ).exclude(review_decision=ReviewDecision.REJECTED)


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
        if self.state in CLEAN_STATES or not self.quarantine_key:
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


class FieldOrigin(models.TextChoices):
    """Where a field value came from (S7.5 provenance). Never shown as verified."""

    XML = "xml", "XML da nota"
    PDF_TEXT = "pdf_text", "Texto do PDF"
    STAFF = "staff", "Revisão da equipe"
    CLIENT = "client", "Informado pelo cliente"


class ExtractedField(ClientRecord):
    """One value read from, or typed about, a document -- never written back into it.

    Machine rows (`xml`, `pdf_text`) are written once per extraction by
    `apps.cycles.extraction.service`; a reviewer's correction is a new `staff` row
    beside the suggestion, so the suggested value, the reviewed value and who said
    which stay side by side. The `Document` and its bytes are never touched (S7.5).
    Firm-only: portal NONE (ops/sql/roles.sql, tests/conftest.py).
    """

    document = models.ForeignKey(
        "obligations.Document", on_delete=models.PROTECT, related_name="fields"
    )
    name = models.CharField(max_length=64)
    value = models.CharField(max_length=255)
    origin = models.CharField(max_length=16, choices=FieldOrigin.choices)
    source_span = models.CharField(max_length=64, blank=True)
    extractor_version = models.CharField(max_length=32, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(ClientRecord.Meta):
        """Tenant-led lookup of one document's fields."""

        indexes: ClassVar[list[models.Index]] = [
            *ClientRecord.Meta.indexes,
            models.Index(
                fields=["tenant", "document", "name"],
                name="extractedfield_document_idx",
            ),
        ]


class Review(ClientRecord):
    """The firm's decision on one submitted file (S7.5 `record_review`).

    Firm-only (S8.2: `cycles_review` is never portal-visible). What the client may
    read -- the decision and the pt-BR reason -- is projected onto the link by the
    same service. `document_sha256` pins the exact bytes the reviewer decided on.
    """

    link = models.ForeignKey(
        EvidenceLink, on_delete=models.PROTECT, related_name="reviews"
    )
    decision = models.CharField(
        max_length=16,
        choices=[
            (ReviewDecision.ACCEPTED.value, ReviewDecision.ACCEPTED.label),
            (ReviewDecision.REJECTED.value, ReviewDecision.REJECTED.label),
        ],
    )
    client_reason = models.TextField(blank=True)
    # S6.3: a document of another company is accepted only when the reviewer records
    # it as a relevant third-party document, with a firm-only reason.
    third_party = models.BooleanField(default=False)
    override_reason = models.TextField(blank=True)
    # T17: for a receipt request, whether the accepted file is usable as payment
    # evidence. Firm-only like the rest of the row; the client sees only the
    # decision projected onto the link.
    payment_relevant = models.BooleanField(default=False)
    corrections_json = models.JSONField(default=list, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(default=timezone.now)
    document_sha256 = models.CharField(max_length=64)

    class Meta(ClientRecord.Meta):
        """One decision per submitted file."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "client", "link"], name="review_link_uniq"
            ),
        ]

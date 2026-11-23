"""Dry-run-first retention: claim in DB, delete bytes outside, redact in DB."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from celery import shared_task
from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone
from storages.backends.s3 import S3Storage

from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.core.retention import RetentionClass
from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.completion_records import CompletionJustification
from apps.cycles.content_redaction import redact_proposal, redact_publication
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence import CLEAN_STATES
from apps.cycles.export_records import EvidenceExport, ExportState
from apps.cycles.models import (
    ActionProposal,
    ClientPublication,
    CycleAmendment,
    DocumentIntake,
    EvidenceLink,
    ExceptionInternal,
    ExtractedField,
    InternalNote,
    RequestInternal,
    Review,
)
from apps.cycles.retention import (
    RetentionAnchor,
    after_years,
    fiscal_expiry,
    ordered_anchors,
    retention_decision,
)
from apps.cycles.retention_records import ContentPurgeClaim
from apps.delivery.models import OutboundMessage
from apps.integrations.models import AuthorityRecord, FirmAuthorization
from apps.integrations.serpro.retention import federal_document_anchors
from apps.obligations.models import AcquisitionOrigin, Document, FiscalSituationReport
from apps.tenants.account_state import export_available
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Only authorized storage descriptors leave a retention transaction.

    `anchors` and `gap` say why a document has its expiry, or why it has none.
    The operator inventory prints them; the purge decision reads only the expiry.
    """

    client_id: UUID
    kind: str
    id: UUID
    expires_at: datetime | None
    keys: tuple[str, ...] = ()
    anchors: tuple[RetentionAnchor, ...] = ()
    gap: str = ""


def _document_anchors(document: Document) -> tuple[tuple[RetentionAnchor, ...], str]:
    """Every verified reference dating an original, or the reason none does."""
    anchors = [
        RetentionAnchor("ciclo", period_end, cycle_id)
        for cycle_id, period_end in EvidenceLink.objects.filter(
            document=document
        ).values_list("request__cycle_id", "request__cycle__period_end")
    ]
    if document.obligation is not None:
        anchors.append(
            RetentionAnchor(
                "obrigacao",
                document.obligation.competence_month,
                document.obligation.pk,
            )
        )
    # Federal references count with the R1 ones; an unresolved one keeps the original.
    federal, gap = federal_document_anchors(document)
    anchors.extend(federal)
    ordered = ordered_anchors(anchors)
    if gap:
        return ordered, gap
    return ordered, "" if ordered else "sem_vinculo"


def _document_retention(
    document: Document,
) -> tuple[datetime | None, tuple[RetentionAnchor, ...], str]:
    """(expiry, anchors, gap): the latest anchored fiscal year, or why none."""
    if document.retention_class != RetentionClass.FISCAL_EVIDENCE:
        return None, (), "classe_sem_prazo"
    anchors, gap = _document_anchors(document)
    expiry = None if gap else fiscal_expiry(anchors)
    if expiry is None:
        return None, anchors, gap
    # A disposable derivative must never outlive its original's retention.
    # Export claim/request and this decision serialize on the same client.
    for export in EvidenceExport.objects.filter(
        client_id=document.client_id,
        state__in=[ExportState.PENDING, ExportState.BUILDING, ExportState.READY],
        index__data__documents__contains=[{"id": str(document.pk)}],
    ):
        derivative_expiry = export.expires_at or export.created_at + timedelta(days=30)
        expiry = max(expiry, derivative_expiry)
    return expiry, anchors, ""


def _claim_keys(document: Document) -> tuple[str, ...]:
    """Return the final and quarantine keys in the order the claim trigger expects.

    The trigger aggregates them `ORDER BY key` under the database collation, which is
    not Python's code-point order: under `en_US` a `quarantine/...` key sorts before
    a random final key that starts with "Z", and a claim in the other order is
    refused as a different storage descriptor.
    """
    final = Document.objects.filter(pk=document.pk).values_list(
        "storage_key", flat=True
    )
    held = (
        DocumentIntake.objects.filter(document=document)
        .exclude(quarantine_key="")
        .values_list("quarantine_key", flat=True)
    )
    return tuple(final.union(held).order_by("storage_key"))


def _candidates(target: Candidate | None = None) -> list[Candidate]:
    """Recheck one identity under its fence rather than the whole inventory."""
    candidates: list[Candidate] = []
    documents = Document.objects.filter(purged_at=None)
    if target is not None:
        documents = (
            documents.filter(pk=target.id, client_id=target.client_id)
            if (target.kind == "document")
            else documents.none()
        )
    for document in documents.select_related("obligation"):
        expiry, anchors, gap = _document_retention(document)
        candidates.append(
            Candidate(
                document.client_id,
                "document",
                document.pk,
                expiry,
                _claim_keys(document),
                anchors,
                gap,
            )
        )
    exports = EvidenceExport.objects.filter(state=ExportState.READY)
    if target is not None:
        exports = (
            exports.filter(pk=target.id, client_id=target.client_id)
            if (target.kind == "export")
            else exports.none()
        )
    candidates.extend(
        Candidate(
            export.client_id,
            "export",
            export.pk,
            export.expires_at,
            (export.storage_key,),
        )
        for export in exports
    )
    proposals = ActionProposal.objects.filter(redacted_at=None)
    if target is not None:
        proposals = (
            proposals.filter(pk=target.id, client_id=target.client_id)
            if (target.kind == "proposal")
            else proposals.none()
        )
    for proposal in proposals:
        retention_years = (
            2
            if proposal.action_kind
            in {ActionKind.EMAIL_NOTICE, ActionKind.CLIENT_PUBLICATION}
            else 5
        )
        candidates.append(
            Candidate(
                proposal.client_id,
                "proposal",
                proposal.pk,
                after_years(proposal.created_at, retention_years),
            )
        )
    candidates.extend(_text_candidates(target))
    return candidates


def _text_candidates(target: Candidate | None) -> list[Candidate]:
    definitions = (
        ("message", OutboundMessage.objects.filter(redacted_at=None), "created_at", 2),
        (
            "publication",
            ClientPublication.objects.filter(redacted_at=None),
            "published_at",
            2,
        ),
        (
            "ai_derived",
            ExtractedField.objects.filter(
                retention_class=RetentionClass.AI_DERIVED, redacted_at=None
            ),
            "created_at",
            1,
        ),
        ("note", InternalNote.objects.exclude(body=""), "created_at", 5),
        ("amendment", CycleAmendment.objects.exclude(reason=""), "created_at", 5),
        (
            "justification",
            CompletionJustification.objects.filter(redacted_at=None),
            "generation__completed_at",
            5,
        ),
        (
            "request_internal",
            RequestInternal.objects.exclude(instructions="", waiver_reason=""),
            "request__created_at",
            5,
        ),
        (
            "exception_internal",
            ExceptionInternal.objects.exclude(next_action="", evidence_refs=[]),
            "exception__created_at",
            5,
        ),
    )
    candidates: list[Candidate] = []
    for kind, base_query, created, years in definitions:
        query = base_query
        if target is not None:
            if kind != target.kind:
                continue
            query = query.filter(pk=target.id, client_id=target.client_id)
        candidates.extend(
            Candidate(client_id, kind, pk, after_years(when, years))
            for client_id, pk, when in query.values_list("client_id", "pk", created)
        )
    return candidates


def _redact(claim: ContentPurgeClaim, now: datetime) -> None:
    """Metadata, hashes and append-only receipts survive content removal."""
    match claim.content_kind:
        case "document":
            Document.objects.filter(pk=claim.content_id).update(purged_at=now)
            ExtractedField.objects.filter(document_id=claim.content_id).update(
                value="", source_span="", redacted_at=now
            )
            Review.objects.filter(link__document_id=claim.content_id).update(
                corrections_json=[], client_reason="", override_reason=""
            )
            EvidenceLink.objects.filter(document_id=claim.content_id).update(
                client_reason=""
            )
        case "export":
            EvidenceExport.objects.filter(pk=claim.content_id).update(
                state=ExportState.EXPIRED
            )
        case "message":
            OutboundMessage.objects.filter(pk=claim.content_id).update(
                subject="", decision_note="", redacted_at=now
            )
        case "proposal":
            redact_proposal(claim.content_id, now)
        case "publication":
            redact_publication(claim.content_id, now)
        case "ai_derived":
            ExtractedField.objects.filter(pk=claim.content_id).update(
                value="", source_span="", redacted_at=now
            )
        case _:
            _redact_private(claim, now)


def _redact_private(claim: ContentPurgeClaim, now: datetime) -> None:
    match claim.content_kind:
        case "note":
            InternalNote.objects.filter(pk=claim.content_id).update(body="")
        case "amendment":
            CycleAmendment.objects.filter(pk=claim.content_id).update(reason="")
        case "justification":
            CompletionJustification.objects.filter(pk=claim.content_id).update(
                reasons={}, redacted_at=now
            )
        case "request_internal":
            RequestInternal.objects.filter(pk=claim.content_id).update(
                instructions="", waiver_reason=""
            )
        case "exception_internal":
            ExceptionInternal.objects.filter(pk=claim.content_id).update(
                next_action="", evidence_refs=[]
            )
        case _:
            message = "Unknown retained content kind."
            raise ValueError(message)


def disposal_refusal(candidate: Candidate) -> str:
    """Name what is unproved about destroying an original; empty when it is safe.

    Authority evidence is never ordinary evidence. A federal original (one SERPRO
    obtained, or one a fiscal-situation report holds) is destroyed only on storage
    whose single stored copy the key delete removes and only after its clean scan:
    a versioned bucket keeps noncurrent versions a client hold cannot protect.
    """
    if candidate.kind != "document":
        return ""
    document = Document.objects.get(pk=candidate.id, client_id=candidate.client_id)
    if (
        AuthorityRecord.objects.filter(evidence_document=document).exists()
        or FirmAuthorization.objects.filter(signed_document=document).exists()
    ):
        return "evidencia_de_autorizacao"
    federal = (
        document.acquisition_origin == AcquisitionOrigin.FEDERAL_PROVIDER
        or FiscalSituationReport.objects.filter(document=document).exists()
    )
    if not federal:
        return ""
    if isinstance(default_storage, S3Storage):
        return "armazenamento_nao_suportado"
    intake = DocumentIntake.objects.filter(document=document).first()
    if intake is not None and intake.state not in CLEAN_STATES:
        return "verificacao_pendente"
    return ""


def _kept_as(candidate: Candidate, now: datetime) -> str:
    """Return the count a retained candidate falls under; empty when it may be claimed.

    The refusal runs before a claim exists and before an open claim (recovery)
    deletes anything, so a refused original keeps its bytes and its evidence.
    """
    decision = retention_decision(candidate.client_id, candidate.expires_at, now=now)
    if not decision.eligible:
        return "held" if decision.reason == "legal_hold" else "retained"
    return "retained" if disposal_refusal(candidate) else ""


def _finish(tid: UUID, client_id: UUID, claim_id: UUID, now: datetime) -> bool:
    """Complete one claim once; an overlapping run that finished it first wins."""
    with tenant_context(tid):
        ClientCompany.objects.select_for_update(no_key=True).get(pk=client_id)
        claim = ContentPurgeClaim.objects.select_for_update().get(pk=claim_id)
        if claim.completed_at is not None:
            return False
        _redact(claim, now)
        claim.completed_at = now
        claim.error_code = ""
        claim.save(update_fields=["completed_at", "error_code"])
        return True


def purge_tenant(tid: UUID, *, dry_run: bool, now: datetime) -> dict[str, int]:
    """Honor holds committed before the deletion claim and retain retry evidence."""
    assert_no_transaction()
    counts = {"eligible": 0, "held": 0, "retained": 0, "purged": 0, "failed": 0}
    with tenant_context(tid):
        if not tenant_has(tid, "cycles_r1"):
            return counts
        candidates = _candidates()
    for candidate in candidates:
        with tenant_context(tid):
            # PLATFORM_QUERY_OK: cancellation and deletion claims share this fence.
            tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tid)
            if tenant.cancellation_ends_on is not None and export_available(
                tenant, on=timezone.localdate(now)
            ):
                counts["retained"] += 1
                continue
            ClientCompany.objects.select_for_update(no_key=True).get(
                pk=candidate.client_id
            )
            fresh_rows = _candidates(candidate)
            if not fresh_rows:
                continue
            fresh = fresh_rows[0]
            kept = _kept_as(fresh, now)
            if kept:
                counts[kept] += 1
                continue
            counts["eligible"] += 1
            if dry_run:
                continue
            claim, _created = ContentPurgeClaim.objects.get_or_create(
                client_id=fresh.client_id,
                content_kind=fresh.kind,
                content_id=fresh.id,
                defaults={"tenant_id": tid, "storage_keys": list(fresh.keys)},
            )
            if claim.completed_at is not None:
                continue
            claim_id, keys = claim.pk, tuple(claim.storage_keys)
        try:
            assert_no_transaction()
            for key in keys:
                default_storage.delete(key)
        except (OSError, BotoCoreError, ClientError):
            counts["failed"] += 1
            with tenant_context(tid):
                ContentPurgeClaim.objects.filter(pk=claim_id).update(
                    error_code="storage_failed"
                )
            continue
        counts["purged"] += int(_finish(tid, candidate.client_id, claim_id, now))
    return counts


def _purge_expired_content(
    self: PlatformTask, *, now: datetime | None = None
) -> dict[str, int]:
    """Apply the owner's dry-run setting and log counts without content."""
    clock = now or timezone.now()
    totals = {"eligible": 0, "held": 0, "retained": 0, "purged": 0, "failed": 0}
    for tid in self.tenant_ids():
        counts = purge_tenant(tid, dry_run=settings.RETENTION_PURGE_DRY_RUN, now=clock)
        for key, value in counts.items():
            totals[key] += value
    logger.info(
        "Retention candidates=%s held=%s retained=%s purged=%s failed=%s",
        totals["eligible"],
        totals["held"],
        totals["retained"],
        totals["purged"],
        totals["failed"],
    )
    return totals


purge_expired_content = shared_task(
    name="apps.cycles.retention_tasks.purge_expired_content",
    base=PlatformTask,
    bind=True,
)(_purge_expired_content)

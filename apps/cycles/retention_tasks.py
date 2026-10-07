"""Dry-run-first retention: claim in DB, delete bytes outside, redact in DB."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from botocore.exceptions import BotoCoreError, ClientError
from celery import shared_task
from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.core.retention import RetentionClass
from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.completion_records import CompletionJustification
from apps.cycles.content_redaction import redact_proposal, redact_publication
from apps.cycles.effects.manifest import ActionKind
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
from apps.cycles.retention import after_years, retention_decision
from apps.cycles.retention_records import ContentPurgeClaim
from apps.delivery.models import OutboundMessage
from apps.obligations.models import Document

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Only authorized storage descriptors leave a retention transaction."""

    client_id: UUID
    kind: str
    id: UUID
    expires_at: datetime | None
    keys: tuple[str, ...] = ()


def _document_expiry(document: Document) -> datetime | None:
    years = list(
        EvidenceLink.objects.filter(document=document).values_list(
            "request__cycle__period_end__year", flat=True
        )
    )
    if document.obligation is not None:
        years.append(document.obligation.competence_month.year)
    expiry = (
        datetime(max(years) + 6, 1, 1, tzinfo=ZoneInfo("America/Sao_Paulo"))
        if years and document.retention_class == RetentionClass.FISCAL_EVIDENCE
        else None
    )
    # A disposable derivative must never outlive its original's retention.
    # Export claim/request and this decision serialize on the same client.
    for export in EvidenceExport.objects.filter(
        client_id=document.client_id,
        state__in=[ExportState.PENDING, ExportState.BUILDING, ExportState.READY],
        index__data__documents__contains=[{"id": str(document.pk)}],
    ):
        derivative_expiry = export.expires_at or export.created_at + timedelta(days=30)
        if expiry is not None:
            expiry = max(expiry, derivative_expiry)
    return expiry


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
        expiry = _document_expiry(document)
        intake = DocumentIntake.objects.filter(document=document).first()
        keys = {document.storage_key}
        if intake is not None and intake.quarantine_key:
            keys.add(intake.quarantine_key)
        candidates.append(
            Candidate(
                document.client_id, "document", document.pk, expiry, tuple(sorted(keys))
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
            ClientCompany.objects.select_for_update(no_key=True).get(
                pk=candidate.client_id
            )
            fresh_rows = _candidates(candidate)
            if not fresh_rows:
                continue
            fresh = fresh_rows[0]
            decision = retention_decision(fresh.client_id, fresh.expires_at, now=now)
            if not decision.eligible:
                counts["held" if decision.reason == "legal_hold" else "retained"] += 1
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
        with tenant_context(tid):
            ClientCompany.objects.select_for_update(no_key=True).get(
                pk=candidate.client_id
            )
            claim = ContentPurgeClaim.objects.select_for_update().get(pk=claim_id)
            _redact(claim, now)
            claim.completed_at = now
            claim.error_code = ""
            claim.save(update_fields=["completed_at", "error_code"])
            counts["purged"] += 1
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

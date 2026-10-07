"""Short authorized database phases for bounded archive jobs."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.core.flags import tenant_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.completion_records import CompletionGeneration
from apps.cycles.completion_sources import lock_index_subjects
from apps.cycles.effects.manifest import JSONValue
from apps.cycles.evidence import CLEAN_STATES
from apps.cycles.evidence_index import EVIDENCE_EXPORT, freeze_index
from apps.cycles.evidence_zip import Original, ZipError, ZipErrorCode
from apps.cycles.export_records import EvidenceExport, ExportAttempt, ExportState
from apps.cycles.index_projection import collect_index, digest_data
from apps.cycles.models import DocumentIntake, ServiceCycle
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.services.approvals import _lock_scope
from apps.cycles.services.cycle import check_version, emit, load_cycle, require_actor
from apps.obligations.models import Document

EXPORT_LEASE = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ExportClaim:
    """No ORM object or lazy relation escapes the claim transaction."""

    tenant_id: UUID
    client_id: UUID
    cycle_id: UUID
    export_id: UUID
    attempt_id: UUID
    fence: int
    storage_key: str
    index_id: UUID
    data: dict[str, JSONValue]
    full_digest: str
    historical: bool
    originals: tuple[Original, ...]


@transaction.atomic
def request_export(
    actor: User, cycle_id: UUID, *, expected_version: int
) -> EvidenceExport:
    """Persist recoverable work; workers, never request transactions, touch storage."""
    from apps.cycles.export_tasks import build_export  # noqa: PLC0415

    candidate = load_cycle(actor, cycle_id)
    scope = _lock_scope(actor, candidate, {actor.pk})
    cycle = scope.cycle
    scope.require(EVIDENCE_EXPORT)
    check_version(cycle.version, expected_version)
    generation = (
        CompletionGeneration.objects.filter(cycle=cycle, amendment=cycle.amendment)
        .select_related("index")
        .first()
    )
    index = generation.index if generation else freeze_index(scope.actor, cycle)
    export = EvidenceExport.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        index=index,
        requested_by=scope.actor,
    )
    cycle.version += 1
    cycle.save(update_fields=["version"])
    emit(
        cycle,
        scope.actor,
        "evidence.export_requested",
        payload={"export_id": str(export.pk)},
    )
    transaction.on_commit(
        lambda: build_export.delay(str(cycle.tenant_id), str(export.pk))
    )
    return export


def claim_export(tid: UUID, export_id: UUID) -> ExportClaim | None:
    """Authorize and commit a fence before any original is opened."""
    assert_no_transaction()
    with tenant_context(tid):
        if not tenant_has(tid, "cycles_r1"):
            return None
        candidate = (
            EvidenceExport.objects.filter(pk=export_id).select_related("cycle").first()
        )
        if candidate is None:
            raise Http404
        actor = User.objects.get(pk=candidate.requested_by_id)
        scope = _lock_scope(actor, candidate.cycle, {actor.pk})
        scope.require(EVIDENCE_EXPORT)
        export = EvidenceExport.objects.select_for_update().get(pk=export_id)
        now = timezone.now()
        if export.state in {ExportState.READY, ExportState.EXPIRED} or (
            export.state == ExportState.BUILDING
            and export.lease_until is not None
            and export.lease_until > now
        ):
            return None
        historical = CompletionGeneration.objects.filter(
            index_id=export.index_id
        ).exists()
        if not historical:
            export.index = freeze_index(scope.actor, scope.cycle)
        index = export.index
        originals = []
        for descriptor in index.data["documents"]:
            document = Document.objects.filter(
                pk=descriptor["id"], client_id=scope.client.pk
            ).first()
            if document is None or document.purged_at is not None:
                raise ZipError(ZipErrorCode.ORIGINAL_UNAVAILABLE)
            if ContentPurgeClaim.objects.filter(
                client_id=scope.client.pk,
                content_kind="document",
                content_id=document.pk,
                completed_at=None,
            ).exists():
                raise ZipError(ZipErrorCode.ORIGINAL_UNAVAILABLE)
            if (
                document.sha256 != descriptor["sha256"]
                or document.byte_size != descriptor["byte_size"]
            ):
                raise ZipError(ZipErrorCode.SOURCE_CHANGED)
            intake = DocumentIntake.objects.filter(document=document).first()
            if (intake is None and document.client_upload) or (
                intake is not None and intake.state not in CLEAN_STATES
            ):
                raise ZipError(ZipErrorCode.ORIGINAL_INELIGIBLE)
            originals.append(
                Original(
                    document.pk,
                    document.storage_key,
                    document.sha256,
                    document.byte_size,
                    document.content_type,
                    document.original_filename,
                )
            )
        export.fence += 1
        export.version += 1
        export.state = ExportState.BUILDING
        export.error_code = ""
        export.lease_until = now + EXPORT_LEASE
        export.save(
            update_fields=[
                "fence",
                "version",
                "state",
                "error_code",
                "lease_until",
                "index",
            ]
        )
        attempt = ExportAttempt(
            tenant_id=tid,
            client_id=scope.client.pk,
            export=export,
            fence=export.fence,
        )
        attempt.storage_key = (
            f"exports/{tid}/{scope.client.pk}/{export.pk}/"
            f"{export.fence}-{attempt.owner}/archive.zip"
        )
        attempt.save()
        return ExportClaim(
            tid,
            scope.client.pk,
            scope.cycle.pk,
            export.pk,
            attempt.pk,
            export.fence,
            attempt.storage_key,
            index.pk,
            index.data,
            digest_data(index.data),
            historical,
            tuple(originals),
        )


def finalize_export(claim: ExportClaim, *, size: int, sha256: str) -> bool:
    """Refuse publication after a lost fence, even if the upload succeeded."""
    assert_no_transaction()
    with tenant_context(claim.tenant_id):
        candidate = EvidenceExport.objects.get(pk=claim.export_id)
        actor = User.objects.get(pk=candidate.requested_by_id)
        cycle = ServiceCycle.objects.get(pk=claim.cycle_id)
        scope = _lock_scope(actor, cycle, {actor.pk})
        scope.require(EVIDENCE_EXPORT)
        export = EvidenceExport.objects.select_for_update().get(pk=claim.export_id)
        if (
            export.fence != claim.fence
            or export.state != ExportState.BUILDING
            or export.index_id != claim.index_id
            or export.client_id != claim.client_id
            or export.lease_until is None
            or export.lease_until <= timezone.now()
        ):
            return False
        lock_index_subjects(scope.cycle)
        current = None
        if not claim.historical:
            current = collect_index(scope.cycle)
            current["generation"] = claim.data["generation"]
        if current is not None and digest_data(current) != claim.full_digest:
            export.state = ExportState.FAILED
            export.error_code = "source_changed"
            export.lease_until = None
            export.version += 1
            export.save(update_fields=["state", "error_code", "version", "lease_until"])
            emit(
                scope.cycle,
                scope.actor,
                "evidence.export_failed",
                payload={
                    "export_id": str(export.pk),
                    "code": export.error_code,
                },
            )
            return False
        export.state = ExportState.READY
        export.storage_key = claim.storage_key
        export.byte_size = size
        export.sha256 = sha256
        export.completed_at = timezone.now()
        export.expires_at = export.completed_at + timedelta(days=30)
        export.lease_until = None
        export.version += 1
        export.save()
        ExportAttempt.objects.filter(pk=claim.attempt_id, fence=claim.fence).update(
            cleanup_pending=False
        )
        emit(
            scope.cycle,
            scope.actor,
            "evidence.export_ready",
            payload={
                "export_id": str(export.pk),
                "digest": sha256,
                "bytes": size,
            },
        )
        return True


def fail_export(
    tid: UUID, export_id: UUID, code: str, *, fence: int | None = None
) -> None:
    """Persist only machine codes and leave a stale winner untouched."""
    with tenant_context(tid):
        query = EvidenceExport.objects.filter(pk=export_id)
        if fence is not None:
            query = query.filter(fence=fence, state=ExportState.BUILDING)
        export = query.select_for_update().first()
        if export is not None and export.state not in {
            ExportState.READY,
            ExportState.EXPIRED,
        }:
            export.state = ExportState.FAILED
            export.error_code = code
            export.lease_until = None
            export.version += 1
            export.save(update_fields=["state", "error_code", "version", "lease_until"])


def load_export(actor: User, export_id: UUID) -> EvidenceExport:
    """Reauthorize every read and download; an archive UUID confers no authority."""
    export = EvidenceExport.objects.filter(pk=export_id).first()
    if export is None:
        raise Http404
    load_cycle(actor, export.cycle_id)
    require_actor(actor, EVIDENCE_EXPORT)
    if not actor.is_active:
        raise PermissionDenied
    return export

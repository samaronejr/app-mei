"""Claim -> bounded storage I/O -> fenced finalize, with owned-key recovery."""

from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from celery import shared_task
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import DatabaseError
from django.db.models import F, Q
from django.http import Http404
from django.utils import timezone

from apps.core.flags import tenant_has
from apps.core.tasks import PhasedTenantTask, PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.archive_storage import upload_archive
from apps.cycles.errors import VersionConflict
from apps.cycles.evidence_zip import Bundle, ZipError, ZipErrorCode, build_archive
from apps.cycles.export_records import EvidenceExport, ExportAttempt, ExportState
from apps.cycles.export_service import (
    EXPORT_LEASE,
    ExportClaim,
    claim_export,
    fail_export,
    finalize_export,
)


def cleanup_attempt(
    tid: UUID, attempt_id: UUID, *, worker_finished: bool = False
) -> bool:
    """Only abandoned attempts own a deletable key; never delete the ready winner."""
    assert_no_transaction()
    with tenant_context(tid):
        query = ExportAttempt.objects.filter(pk=attempt_id)
        if not worker_finished:
            query = query.filter(
                cleanup_pending=True, created_at__lte=timezone.now() - EXPORT_LEASE
            )
        attempt = query.first()
        if attempt is None:
            return True
        export = EvidenceExport.objects.get(pk=attempt.export_id)
        if (
            export.state == ExportState.READY
            and export.storage_key == attempt.storage_key
        ):
            return False
        if not worker_finished and (
            export.state == ExportState.BUILDING
            and export.fence == attempt.fence
            and export.lease_until is not None
            and export.lease_until > timezone.now()
        ):
            return False
        key = attempt.storage_key
    try:
        default_storage.delete(key)
    except (OSError, BotoCoreError, ClientError):
        return False
    with tenant_context(tid):
        ExportAttempt.objects.filter(pk=attempt_id, storage_key=key).update(
            cleanup_pending=False
        )
    return True


def _claim_or_fail(tid: UUID, eid: UUID) -> ExportClaim | None:
    try:
        return claim_export(tid, eid)
    except (PermissionDenied, Http404):
        fail_export(tid, eid, "authorization_changed")
    except VersionConflict:
        fail_export(tid, eid, "source_busy")
    except ZipError as failure:
        fail_export(tid, eid, failure.code)
    return None


def _upload(claim: ExportClaim, bundle: Bundle) -> None:
    saved = upload_archive(default_storage, claim.storage_key, bundle)
    if saved != claim.storage_key:
        if saved.startswith(claim.storage_key.rsplit("/", 1)[0] + "/"):
            with tenant_context(claim.tenant_id):
                ExportAttempt.objects.filter(pk=claim.attempt_id).update(
                    storage_key=saved
                )
        raise ZipError(ZipErrorCode.ALTERNATE_STORAGE_NAME)


def _build_export(tenant_id: str, export_id: str) -> None:
    """Invoke the task object, not .run(): the base rejects outer transactions."""
    tid, eid = UUID(tenant_id), UUID(export_id)
    claim = _claim_or_fail(tid, eid)
    if claim is None:
        return
    won = False
    database_failed = False
    try:
        assert_no_transaction()
        with build_archive(
            claim.data, claim.originals, storage=default_storage
        ) as bundle:
            _upload(claim, bundle)
            won = finalize_export(claim, size=bundle.byte_size, sha256=bundle.sha256)
    except ZipError as failure:
        fail_export(tid, eid, failure.code, fence=claim.fence)
    except (PermissionDenied, Http404):
        fail_export(tid, eid, "authorization_changed", fence=claim.fence)
    except VersionConflict:
        fail_export(tid, eid, "source_busy", fence=claim.fence)
    except (OSError, BotoCoreError, ClientError):
        fail_export(tid, eid, "storage_failed", fence=claim.fence)
    except DatabaseError:
        # The attempt remains durable. A later sweep cleans it after the DB recovers.
        database_failed = True
        raise
    finally:
        if not won and not database_failed:
            cleanup_attempt(tid, claim.attempt_id, worker_finished=True)


build_export = shared_task(
    name="apps.cycles.export_tasks.build_export",
    base=PhasedTenantTask,
    time_limit=600,
    soft_time_limit=540,
)(_build_export)


def _sweep_evidence_exports(self: PlatformTask) -> int:
    """Recover pending work and abandoned attempts only in enabled tenants."""
    count = 0
    for tid in self.tenant_ids():
        with tenant_context(tid):
            if not tenant_has(tid, "cycles_r1"):
                continue
            now = timezone.now()
            # This fence is later than the task's explicit hard deadline. A late
            # inline caller still cleans its own key in finally regardless of markers.
            EvidenceExport.objects.filter(
                state=ExportState.BUILDING, lease_until__lte=now
            ).update(
                state=ExportState.PENDING,
                fence=F("fence") + 1,
                version=F("version") + 1,
                lease_until=None,
            )
            abandoned = list(
                ExportAttempt.objects.filter(
                    cleanup_pending=True, created_at__lte=now - EXPORT_LEASE
                )
                .exclude(
                    Q(export__state=ExportState.READY)
                    & Q(storage_key=F("export__storage_key"))
                )
                .values_list("pk", flat=True)
            )
            pending = list(
                EvidenceExport.objects.filter(state=ExportState.PENDING).values_list(
                    "pk", flat=True
                )
            )
        for attempt_id in abandoned:
            cleanup_attempt(tid, attempt_id)
        for export_id in pending:
            build_export.delay(str(tid), str(export_id))
            count += 1
    return count


sweep_evidence_exports = shared_task(
    name="apps.cycles.export_tasks.sweep_evidence_exports",
    base=PlatformTask,
    bind=True,
)(_sweep_evidence_exports)

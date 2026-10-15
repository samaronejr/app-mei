"""Phased account CSV archives with the T18 byte cap and exact-key recovery."""

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
from apps.cycles.evidence_zip import (
    Bundle,
    ZipError,
    ZipErrorCode,
    build_metadata_archive,
)
from apps.cycles.export_service import EXPORT_LEASE
from apps.tenants.export_service import (
    AccountClaim,
    archive_members,
    claim_export,
    fail_export,
    finalize_export,
)
from apps.tenants.models import AccountExport, AccountExportAttempt, AccountExportState


def cleanup_attempt(
    tenant_id: UUID, attempt_id: UUID, *, worker_finished: bool = False
) -> bool:
    """Delete only an abandoned attempt's exact key, never a published winner."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        query = AccountExportAttempt.objects.filter(pk=attempt_id)
        if not worker_finished:
            query = query.filter(
                cleanup_pending=True, created_at__lte=timezone.now() - EXPORT_LEASE
            )
        attempt = query.first()
        if attempt is None:
            return True
        export = AccountExport.objects.get(pk=attempt.export_id)
        if (
            export.state == AccountExportState.READY
            and export.storage_key == attempt.storage_key
        ):
            return False
        if not worker_finished and (
            export.state == AccountExportState.BUILDING
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
    with tenant_context(tenant_id):
        AccountExportAttempt.objects.filter(pk=attempt_id, storage_key=key).update(
            cleanup_pending=False
        )
    return True


def _upload(claim: AccountClaim, bundle: Bundle) -> None:
    saved = upload_archive(default_storage, claim.storage_key, bundle)
    if saved != claim.storage_key:
        if saved.startswith(claim.storage_key.rsplit("/", 1)[0] + "/"):
            with tenant_context(claim.tenant_id):
                AccountExportAttempt.objects.filter(pk=claim.attempt_id).update(
                    storage_key=saved
                )
        raise ZipError(ZipErrorCode.ALTERNATE_STORAGE_NAME)


def _build_account_export(tenant_id: str, export_id: str) -> None:
    """Reject outer transactions; separate claim, paged reads, storage and finalize."""
    tid, eid = UUID(tenant_id), UUID(export_id)
    try:
        claim = claim_export(tid, eid)
    except (PermissionDenied, Http404):
        fail_export(tid, eid, "authorization_changed")
        return
    if claim is None:
        return
    won = False
    database_failed = False
    try:
        with build_metadata_archive(archive_members(claim)) as bundle:
            _upload(claim, bundle)
            won = finalize_export(claim, size=bundle.byte_size, sha256=bundle.sha256)
    except ZipError as failure:
        fail_export(tid, eid, failure.code, fence=claim.fence)
    except (PermissionDenied, Http404):
        fail_export(tid, eid, "authorization_changed", fence=claim.fence)
    except (OSError, BotoCoreError, ClientError):
        fail_export(tid, eid, "storage_failed", fence=claim.fence)
    except DatabaseError:
        database_failed = True
        raise
    finally:
        if not won and not database_failed:
            cleanup_attempt(tid, claim.attempt_id, worker_finished=True)


build_account_export = shared_task(
    name="apps.tenants.export_tasks.build_account_export",
    base=PhasedTenantTask,
    time_limit=600,
    soft_time_limit=540,
)(_build_account_export)


def _sweep_account_exports(self: PlatformTask) -> int:
    """Recover expired workers and retire archive bytes after their download TTL."""
    count = 0
    for tid in self.tenant_ids():
        with tenant_context(tid):
            if not tenant_has(tid, "firm_settings_r1"):
                continue
            now = timezone.now()
            AccountExport.objects.filter(
                state=AccountExportState.BUILDING, lease_until__lte=now
            ).update(
                state=AccountExportState.PENDING,
                fence=F("fence") + 1,
                version=F("version") + 1,
                lease_until=None,
            )
            expired = AccountExport.objects.filter(
                state=AccountExportState.READY, expires_at__lte=now
            )
            AccountExportAttempt.objects.filter(export__in=expired).update(
                cleanup_pending=True
            )
            expired.update(state=AccountExportState.EXPIRED, version=F("version") + 1)
            abandoned = list(
                AccountExportAttempt.objects.filter(
                    cleanup_pending=True, created_at__lte=now - EXPORT_LEASE
                )
                .exclude(
                    Q(export__state=AccountExportState.READY)
                    & Q(storage_key=F("export__storage_key"))
                )
                .values_list("pk", flat=True)
            )
            pending = list(
                AccountExport.objects.filter(
                    state=AccountExportState.PENDING
                ).values_list("pk", flat=True)
            )
        for attempt_id in abandoned:
            cleanup_attempt(tid, attempt_id)
        for export_id in pending:
            build_account_export.delay(str(tid), str(export_id))
            count += 1
    return count


sweep_account_exports = shared_task(
    name="apps.tenants.export_tasks.sweep_account_exports",
    base=PlatformTask,
    bind=True,
    ignore_result=True,
)(_sweep_account_exports)

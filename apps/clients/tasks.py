"""Background work for the client registry: committing a planned import.

A commit of up to 5,000 rows is a Celery `TenantTask`, so the upload request returns
at once and the batch page shows progress. The task body runs inside the tenant's
context (`TenantTask.__call__`), and `commit_import` re-checks the actor's import
capability under the batch lock — the task is handed ids, never trust.
"""

import logging
from uuid import UUID

import sentry_sdk
from celery import shared_task
from django.db import transaction

from apps.accounts.models import User
from apps.clients.importing import ImportConflictError, commit_import
from apps.clients.models import ImportBatch, ImportStatus
from apps.core.tasks import TenantTask

logger = logging.getLogger(__name__)


@shared_task(base=TenantTask, bind=False, name="clients.commit_import")
def commit_import_task(
    tenant_id: str,  # noqa: ARG001 - consumed by TenantTask to enter the context
    batch_id: str,
    actor_id: str,
    expected_version: int,
) -> dict[str, object]:
    """Commit one queued batch; on an unexpected failure, hand it back for retry.

    The commit runs in its own savepoint, so a crash rolls back every row it wrote
    and leaves nothing half-imported. The batch is then returned to `planned` (and
    its version advanced) so the page offers the commit again instead of showing a
    queue that will never drain. The failure is logged and reported, not swallowed.
    """
    # PLATFORM_QUERY_OK: accounts_user is global; the actor is re-authorized by
    # commit_import inside the tenant's context.
    actor = User.objects.get(pk=actor_id)
    try:
        with transaction.atomic():
            result = commit_import(
                actor,
                UUID(batch_id),
                expected_version=expected_version,
            )
    except ImportConflictError as conflict:
        # A redelivered or duplicate task for a batch that already moved on (acks_late
        # redelivers after a worker loss). Benign: nothing was written, nothing to
        # hand back, and reporting it as a failure would page someone for a no-op.
        logger.info(
            "client import commit skipped: %s",
            conflict,
            extra={"batch_id": batch_id},
        )
        return {"batch_id": batch_id, "status": "conflict"}
    except Exception:
        logger.exception("client import commit failed", extra={"batch_id": batch_id})
        sentry_sdk.capture_exception()
        with transaction.atomic():
            batch = (
                ImportBatch.objects.select_for_update()
                .filter(pk=batch_id, status=ImportStatus.QUEUED)
                .first()
            )
            if batch is not None:
                # A retry of a partial batch keeps its committed rows; hand it back
                # as partial so it can still be rolled back.
                already = batch.rows.filter(client__isnull=False).exists()
                batch.status = ImportStatus.PARTIAL if already else ImportStatus.PLANNED
                batch.version += 1
                batch.save(update_fields=["status", "version"])
        return {"batch_id": batch_id, "status": "failed"}
    return {
        "batch_id": batch_id,
        "status": str(result.status),
        "created": result.created,
        "failed": result.failed,
        "skipped": result.skipped,
    }

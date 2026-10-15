"""Owner-authorized account export claims and bounded scalar CSV pages."""

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from celery import current_app
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.firm_account import require_account_owner
from apps.clients.exports import defuse_formula
from apps.clients.models import ClientCompany, Engagement
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.export_service import EXPORT_LEASE
from apps.cycles.models import EvidenceRequest, ServiceCycle
from apps.delivery.models import OutboundMessage
from apps.obligations.models import Document
from apps.tenants.account_state import export_available
from apps.tenants.models import AccountExport, AccountExportAttempt, AccountExportState
from apps.tenants.services import check_settings_version, lock_firm_settings

type ExportModel = type[
    ClientCompany
    | Engagement
    | ServiceCycle
    | EvidenceRequest
    | OutboundMessage
    | Document
]
TABLES: tuple[tuple[str, ExportModel, tuple[str, ...]], ...] = (
    (
        "clients.csv",
        ClientCompany,
        ("id", "cnpj", "cpf", "legal_name", "trade_name", "status"),
    ),
    (
        "engagements.csv",
        Engagement,
        ("id", "client_id", "service_kind", "starts_on", "ends_on", "version"),
    ),
    (
        "cycles.csv",
        ServiceCycle,
        ("id", "client_id", "engagement_id", "period_start", "period_end", "state"),
    ),
    (
        "requests.csv",
        EvidenceRequest,
        ("id", "client_id", "cycle_id", "title", "state", "due_on"),
    ),
    (
        "messages.csv",
        OutboundMessage,
        ("id", "client_id", "cycle_id", "kind", "state", "created_at", "redacted_at"),
    ),
    (
        "documents.csv",
        Document,
        (
            "id",
            "client_id",
            "original_filename",
            "sha256",
            "byte_size",
            "content_type",
            "purged_at",
        ),
    ),
)
PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class AccountClaim:
    """Immutable identities, not lazy ORM objects, cross the storage boundary."""

    tenant_id: UUID
    export_id: UUID
    actor_id: UUID
    attempt_id: UUID
    fence: int
    storage_key: str
    captured_at: datetime


@transaction.atomic
def request_export(
    actor: User, tenant_id: UUID, *, expected_version: int
) -> AccountExport:
    """Commit a recoverable job even when the account is otherwise read-only."""
    fresh, tenant = lock_firm_settings(actor, tenant_id)
    require_account_owner(fresh, tenant_id)
    check_settings_version(tenant.settings_version, expected_version)
    if not export_available(tenant):
        raise Http404
    export = AccountExport.objects.create(tenant=tenant, requested_by=fresh)
    tenant.settings_version += 1
    tenant.save(update_fields=["settings_version"])
    record_event(
        action="tenant.export_requested",
        actor=fresh,
        obj=ObjectRef("AccountExport", str(export.pk)),
        metadata={"version": tenant.settings_version},
    )
    transaction.on_commit(
        lambda: current_app.send_task(
            "apps.tenants.export_tasks.build_account_export",
            args=[str(tenant_id), str(export.pk)],
        )
    )
    return export


def claim_export(tenant_id: UUID, export_id: UUID) -> AccountClaim | None:
    """Commit the owner recheck and a unique attempt before opening storage."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        candidate = AccountExport.objects.filter(pk=export_id).first()
        if candidate is None:
            raise Http404
        actor = User.objects.get(pk=candidate.requested_by_id)
        fresh, tenant = lock_firm_settings(actor, tenant_id)
        require_account_owner(fresh, tenant_id)
        if not export_available(tenant):
            raise Http404
        export = AccountExport.objects.select_for_update().get(pk=export_id)
        now = timezone.now()
        if export.state != AccountExportState.PENDING:
            return None
        export.state = AccountExportState.BUILDING
        export.fence += 1
        export.version += 1
        export.captured_at = now
        export.lease_until = now + EXPORT_LEASE
        export.save(
            update_fields=["state", "fence", "version", "captured_at", "lease_until"]
        )
        attempt = AccountExportAttempt(
            tenant_id=tenant_id, export=export, fence=export.fence
        )
        attempt.storage_key = (
            f"account-exports/{tenant_id}/{export.pk}/"
            f"{export.fence}-{attempt.owner}/archive.zip"
        )
        attempt.save()
        return AccountClaim(
            tenant_id,
            export.pk,
            fresh.pk,
            attempt.pk,
            export.fence,
            attempt.storage_key,
            now,
        )


def _csv_rows(
    claim: AccountClaim, model: ExportModel, columns: tuple[str, ...]
) -> Iterator[bytes]:
    """Fetch at most one scalar page in a transaction, then encode outside it."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(columns)
    yield ("\ufeff" + buffer.getvalue()).encode()
    last_id: UUID | None = None
    while True:
        with tenant_context(claim.tenant_id):
            query = model.objects.filter(created_at__lte=claim.captured_at)
            if last_id is not None:
                query = query.filter(pk__gt=last_id)
            rows = list(query.order_by("pk").values_list(*columns)[:PAGE_SIZE])
        if not rows:
            return
        for row in rows:
            buffer.seek(0)
            buffer.truncate()
            writer.writerow(
                defuse_formula("" if value is None else str(value)) for value in row
            )
            yield buffer.getvalue().encode()
        last_id = rows[-1][0]


def archive_members(claim: AccountClaim) -> Iterator[tuple[str, Iterator[bytes]]]:
    """Export metadata only, with capture semantics and no storage keys or secrets."""
    for name, model, columns in TABLES:
        yield name, _csv_rows(claim, model, columns)
    description = {
        "tenant_id": str(claim.tenant_id),
        "export_id": str(claim.export_id),
        "capture_started_at": claim.captured_at.isoformat(),
        "capture_finished_at": timezone.now().isoformat(),
        "consistency": "paged_current_values_created_before_capture",
        "originals_included": False,
        "documents": "documents.csv is the original-file metadata manifest",
    }
    yield "export.json", iter((json.dumps(description, sort_keys=True).encode(),))


def finalize_export(claim: AccountClaim, *, size: int, sha256: str) -> bool:
    """Publish only the current fence under fresh owner and cancellation authority."""
    assert_no_transaction()
    with tenant_context(claim.tenant_id):
        actor = User.objects.get(pk=claim.actor_id)
        fresh, tenant = lock_firm_settings(actor, claim.tenant_id)
        require_account_owner(fresh, claim.tenant_id)
        if not export_available(tenant):
            raise Http404
        export = AccountExport.objects.select_for_update().get(pk=claim.export_id)
        if (
            export.fence != claim.fence
            or export.state != AccountExportState.BUILDING
            or export.lease_until is None
            or export.lease_until <= timezone.now()
        ):
            return False
        export.state = AccountExportState.READY
        export.storage_key = claim.storage_key
        export.byte_size = size
        export.sha256 = sha256
        export.completed_at = timezone.now()
        export.expires_at = export.completed_at + timedelta(days=30)
        export.lease_until = None
        export.version += 1
        export.save()
        AccountExportAttempt.objects.filter(pk=claim.attempt_id).update(
            cleanup_pending=False
        )
        record_event(
            action="tenant.export_ready",
            actor=fresh,
            obj=ObjectRef("AccountExport", str(export.pk)),
            metadata={"digest": sha256, "bytes": size, "fence": claim.fence},
        )
        return True


def fail_export(
    tenant_id: UUID, export_id: UUID, code: str, *, fence: int | None = None
) -> None:
    """Keep only a bounded failure code; never overwrite a newer ready winner."""
    with tenant_context(tenant_id):
        query = AccountExport.objects.filter(pk=export_id)
        if fence is not None:
            query = query.filter(fence=fence, state=AccountExportState.BUILDING)
        export = query.select_for_update().first()
        if export is None or export.state in {
            AccountExportState.READY,
            AccountExportState.EXPIRED,
        }:
            return
        export.state = AccountExportState.FAILED
        export.error_code = code
        export.lease_until = None
        export.version += 1
        export.save(update_fields=["state", "error_code", "lease_until", "version"])
        record_event(
            action="tenant.export_failed",
            obj=ObjectRef("AccountExport", str(export.pk)),
            metadata={"code": code, "fence": export.fence},
        )

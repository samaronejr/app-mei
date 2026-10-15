"""Preview, confirm, control and export a frozen, portfolio-scoped bulk operation."""

import csv
import io
from datetime import timedelta
from typing import Final
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.portfolio import visible_clients
from apps.authz.services import can
from apps.clients.exports import UTF8_BOM, defuse_formula
from apps.core.flags import tenant_has
from apps.cycles.bulk import (
    ACTIVE_OPERATIONS,
    BULK_CAPABILITY,
    BulkItem,
    BulkItemState,
    BulkKind,
    BulkOperation,
    BulkOperationState,
)
from apps.cycles.bulk_contracts import (
    PreviewExpectation,
    Selection,
    SnapshotItem,
    digest,
    list_value,
    snapshot_items,
)
from apps.cycles.bulk_effects import preview_item
from apps.cycles.effects.manifest import JSONValue, utc_timestamp
from apps.cycles.errors import VersionConflict
from apps.cycles.services.cycle import check_version, require_enabled, tenant_id

PREVIEW_LIFETIME: Final = timedelta(minutes=15)


def require_bulk(actor: User) -> None:
    """Apply the same rollout and authority to direct service calls and views."""
    require_enabled()
    if not tenant_has(tenant_id(), "bulk_r1"):
        raise Http404
    if (
        not actor.is_active
        or actor.is_superuser
        or not can(actor, BULK_CAPABILITY, tenant_id=tenant_id())
    ):
        raise PermissionDenied


def record_bulk(
    operation: BulkOperation,
    event: str,
    *,
    actor: User | None = None,
    metadata: dict[str, str | int] | None = None,
) -> None:
    """Keep only ids, hashes and codes in the append-only audit."""
    record_event(
        action=event,
        tenant_id=operation.tenant_id,
        actor=actor,
        obj=ObjectRef("BulkOperation", str(operation.pk)),
        metadata={
            "version": operation.version,
            "snapshot_digest": operation.snapshot_digest,
            **(metadata or {}),
        },
    )


def checked_items(operation: BulkOperation) -> tuple[SnapshotItem, ...]:
    """Reject digest mismatches rather than inventing a new selection or success."""
    if operation.snapshot_digest != digest(operation.snapshot_json):
        msg = "A prévia perdeu sua integridade. Gere uma nova prévia."
        raise VersionConflict(msg)
    return snapshot_items(operation.snapshot_json)


def load_operation(
    actor: User, operation_id: UUID, *, lock: bool = False
) -> BulkOperation:
    """Fail the whole read closed when any frozen client leaves the user's book."""
    require_bulk(actor)
    query = BulkOperation.objects.all()
    if lock:
        query = query.select_for_update()
    operation = get_object_or_404(query, pk=operation_id)
    ids = {row.client_id for row in checked_items(operation)}
    visible = set(
        visible_clients(actor, tenant_id=tenant_id())
        .filter(pk__in=ids)
        .values_list("pk", flat=True)
    )
    if ids != visible:
        raise Http404
    # Repeat after the aggregate lock; request decorators are only admission hints.
    require_bulk(actor)
    return operation


@transaction.atomic
def preview_bulk(actor: User, op: BulkKind, selection: Selection) -> BulkOperation:
    """Persist a rehearsal, but create no cycles, requests, items or approvals."""
    require_bulk(actor)
    selection.validate()
    if op == BulkKind.OPEN_CYCLES and selection.period is None:
        msg = "Informe a competência."
        raise ValidationError(msg)
    if op in {BulkKind.OPEN_CYCLES, BulkKind.MIGRATE_OPEN_CYCLES} and (
        selection.playbook_version_id is None
    ):
        msg = "Selecione uma versão publicada."
        raise ValidationError(msg)
    now = timezone.now()
    operation = BulkOperation(
        tenant_id=tenant_id(),
        requested_by=actor,
        kind=op,
        created_at=now,
        valid_until=now + PREVIEW_LIFETIME,
    )
    items = [preview_item(actor, op, pk, selection) for pk in selection.subject_ids]
    snapshot: dict[str, JSONValue] = {
        "schema": "axorim.bulk.v1",
        "operation_id": str(operation.pk),
        "tenant_id": str(tenant_id()),
        "requested_by": str(actor.pk),
        "kind": op,
        "computed_at": utc_timestamp(now),
        "valid_until": utc_timestamp(operation.valid_until),
        "items": [item.as_data() for item in items],
        "estimated_messages": sum(
            len(list_value(item.effect["recipients"]))
            for item in items
            if item.eligible and op == BulkKind.SEND_REQUEST_NOTICES
        ),
        "estimated_cost": None,
    }
    operation.snapshot_json = snapshot
    operation.snapshot_digest = digest(snapshot)
    operation.idempotency_key = digest([str(tenant_id()), str(operation.pk)])
    operation.save(force_insert=True)
    record_bulk(
        operation, "bulk.previewed", actor=actor, metadata={"items": len(items)}
    )
    return operation


@transaction.atomic
def run_bulk(
    actor: User,
    snapshot_id: UUID,
    *,
    confirm_count: int,
    exclusions: tuple[str, ...],
    expectation: PreviewExpectation,
) -> BulkOperation:
    """Queue exactly the confirmed frozen subjects; the recovery sweep owns dispatch."""
    operation = load_operation(actor, snapshot_id, lock=True)
    check_version(operation.version, expectation.version)
    if operation.requested_by_id != actor.pk:
        raise PermissionDenied
    if (
        operation.state != BulkOperationState.PREVIEW
        or operation.snapshot_digest != expectation.digest
        or timezone.now() >= operation.valid_until
    ):
        msg = "Esta prévia expirou ou já foi confirmada. Gere outra para continuar."
        raise VersionConflict(msg)
    items = checked_items(operation)
    known = {str(item.subject_ref) for item in items}
    excluded = set(exclusions)
    if len(excluded) != len(exclusions) or not excluded.issubset(known):
        msg = "As exclusões precisam pertencer a esta prévia, sem repetições."
        raise ValidationError(msg)
    count = sum(
        item.eligible and str(item.subject_ref) not in excluded for item in items
    )
    if type(confirm_count) is not int or count == 0 or confirm_count != count:
        msg = (
            f"Digite exatamente {count} para confirmar "
            "os itens elegíveis não excluídos."
        )
        raise ValidationError(msg)
    now = timezone.now()
    for position, item in enumerate(items):
        state = (
            BulkItemState.SKIPPED_EXCLUDED
            if str(item.subject_ref) in excluded
            else BulkItemState.PENDING
            if item.eligible
            else BulkItemState.SKIPPED_INELIGIBLE
        )
        BulkItem.objects.create(
            tenant_id=operation.tenant_id,
            client_id=item.client_id,
            operation=operation,
            subject_ref=str(item.subject_ref),
            expected_version=item.expected_version,
            effect_key=digest(
                [
                    str(operation.tenant_id),
                    str(item.client_id),
                    str(operation.pk),
                    str(item.subject_ref),
                ]
            ),
            position=position,
            state=state,
            result_json={"code": item.reason} if not item.eligible else {},
            finished_at=now if state != BulkItemState.PENDING else None,
        )
    require_bulk(actor)
    confirmed_at = timezone.now()
    if confirmed_at >= operation.valid_until:
        msg = (
            "A prévia expirou durante a confirmação. Nenhum item foi colocado na fila."
        )
        raise VersionConflict(msg)
    operation.state = BulkOperationState.QUEUED
    operation.confirmed_at = confirmed_at
    operation.version += 1
    operation.save(update_fields=["state", "confirmed_at", "version"])
    record_bulk(operation, "bulk.confirmed", actor=actor, metadata={"items": count})
    return operation


@transaction.atomic
def cancel_bulk(actor: User, op_id: UUID, *, expected_version: int) -> BulkOperation:
    """Serialize the stop request with claims; effects already committed stay."""
    operation = load_operation(actor, op_id, lock=True)
    check_version(operation.version, expected_version)
    if operation.state not in ACTIVE_OPERATIONS:
        msg = "Este lote já está encerrado."
        raise VersionConflict(msg)
    operation.state = BulkOperationState.CANCELLING
    operation.version += 1
    operation.save(update_fields=["state", "version"])
    record_bulk(operation, "bulk.cancel_requested", actor=actor)
    return operation


@transaction.atomic
def resume_bulk(actor: User, op_id: UUID, *, expected_version: int) -> BulkOperation:
    """Redispatch only unfinished work; never revive cancelled or terminal effects."""
    operation = load_operation(actor, op_id, lock=True)
    check_version(operation.version, expected_version)
    if operation.state not in {BulkOperationState.QUEUED, BulkOperationState.RUNNING}:
        msg = "Este lote não pode ser retomado. Gere uma nova prévia."
        raise VersionConflict(msg)
    operation.version += 1
    operation.save(update_fields=["version"])
    record_bulk(operation, "bulk.resume_requested", actor=actor)
    return operation


def bulk_report_csv(actor: User, op_id: UUID) -> HttpResponse:
    """Materialize the entire formula-safe report while the tenant context is live."""
    operation = load_operation(actor, op_id)
    results = {
        (row.client_id, row.subject_ref): row
        for row in operation.items.order_by("position")
    }
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(["cliente", "referencia", "estado", "motivo"])
    for item in checked_items(operation):
        result = results.get((item.client_id, str(item.subject_ref)))
        values = (
            item.label,
            str(item.subject_ref),
            result.state if result else BulkOperationState.PREVIEW,
            str(result.result_json.get("code", "")) if result else item.reason,
        )
        writer.writerow([defuse_formula(value) for value in values])
    response = HttpResponse(
        UTF8_BOM + output.getvalue(), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="lote-{operation.pk}.csv"'
    return response

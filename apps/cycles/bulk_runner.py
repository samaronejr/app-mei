"""Short durable claims, one transaction per effect, and monotonic item fences."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.core.flags import tenant_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import (
    ACTIVE_OPERATIONS,
    UNFINISHED_ITEMS,
    BulkItem,
    BulkItemState,
    BulkKind,
    BulkOperation,
    BulkOperationState,
)
from apps.cycles.bulk_effects import execute_effect
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.services.bulk import checked_items, record_bulk
from apps.delivery.errors import IntentConflict

LEASE: Final = timedelta(minutes=2)


def _valid_preview(operation: BulkOperation) -> None:
    if timezone.now() >= operation.valid_until:
        msg = "A prévia expirou antes de concluir este item."
        raise VersionConflict(msg)


class StaleBulkClaimError(VersionConflict):
    """A resumed worker owns a newer fence, or cancellation already won."""


@dataclass(frozen=True, slots=True)
class BulkClaim:
    """The complete claim identity; an item UUID alone is not execution authority."""

    tenant_id: UUID
    client_id: UUID
    operation_id: UUID
    subject_ref: str
    item_id: UUID
    owner: UUID
    fence: int


def _finish(operation: BulkOperation) -> None:
    if operation.state == BulkOperationState.CANCELLING:
        count = operation.items.filter(state__in=UNFINISHED_ITEMS).update(
            state=BulkItemState.CANCELLED,
            finished_at=timezone.now(),
            result_json={"code": "cancelled"},
        )
        state = BulkOperationState.CANCELLED
        record_bulk(operation, "bulk.items_cancelled", metadata={"items": count})
    else:
        if operation.items.filter(state__in=UNFINISHED_ITEMS).exists():
            return
        state = (
            BulkOperationState.COMPLETED_WITH_FAILURES
            if operation.items.filter(state=BulkItemState.FAILED).exists()
            else BulkOperationState.COMPLETED
        )
    operation.state = state
    operation.finished_at = timezone.now()
    operation.version += 1
    operation.save(update_fields=["state", "finished_at", "version"])
    record_bulk(operation, "bulk.finished")


def claim_next(tenant_id: UUID, operation_id: UUID) -> BulkClaim | None:
    """Commit one fenced claim; recovery queries only stored operation/item states."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        if not tenant_has(tenant_id, "cycles_r1") or not tenant_has(
            tenant_id, "bulk_r1"
        ):
            return None
        operation = (
            BulkOperation.objects.select_for_update().filter(pk=operation_id).first()
        )
        if operation is None or operation.state not in ACTIVE_OPERATIONS:
            return None
        checked_items(operation)
        if operation.state == BulkOperationState.CANCELLING:
            _finish(operation)
            return None
        now = timezone.now()
        item = (
            BulkItem.objects.select_for_update(skip_locked=True)
            .filter(operation=operation, state__in=UNFINISHED_ITEMS)
            .filter(
                Q(state=BulkItemState.PENDING)
                | Q(state=BulkItemState.CLAIMED, lease_until__lte=now)
            )
            .order_by("position")
            .first()
        )
        if item is None:
            _finish(operation)
            return None
        item.fence += 1
        item.lease_owner = uuid4()
        item.lease_until = now + LEASE
        item.state = BulkItemState.CLAIMED
        item.save(update_fields=["fence", "lease_owner", "lease_until", "state"])
        if operation.state == BulkOperationState.QUEUED:
            operation.state = BulkOperationState.RUNNING
            operation.version += 1
            operation.save(update_fields=["state", "version"])
        record_bulk(
            operation,
            "bulk.item_claimed",
            metadata={"item_id": str(item.pk), "fence": item.fence},
        )
        return BulkClaim(
            tenant_id,
            item.client_id,
            operation.pk,
            item.subject_ref,
            item.pk,
            item.lease_owner,
            item.fence,
        )


def execute_claim(claim: BulkClaim) -> None:
    """Commit the database effect with its receipt, or persist one honest refusal."""
    assert_no_transaction()
    with tenant_context(claim.tenant_id):
        operation = BulkOperation.objects.select_for_update().get(pk=claim.operation_id)
        item = (
            BulkItem.objects.select_for_update()
            .filter(
                pk=claim.item_id,
                client_id=claim.client_id,
                operation_id=claim.operation_id,
                subject_ref=claim.subject_ref,
            )
            .first()
        )
        if (
            item is None
            or item.state != BulkItemState.CLAIMED
            or item.fence != claim.fence
            or item.lease_owner != claim.owner
            or item.lease_until is None
            or item.lease_until <= timezone.now()
        ):
            msg = "A reserva deste item não pertence mais a este processo."
            raise StaleBulkClaimError(msg)
        if operation.state == BulkOperationState.CANCELLING:
            _finish(operation)
            return
        if not tenant_has(claim.tenant_id, "cycles_r1") or not tenant_has(
            claim.tenant_id, "bulk_r1"
        ):
            return
        selected = checked_items(operation)[item.position]
        if (
            str(selected.subject_ref) != item.subject_ref
            or selected.client_id != item.client_id
            or selected.expected_version != item.expected_version
            or not selected.eligible
        ):
            msg = "O item não corresponde à prévia confirmada."
            raise VersionConflict(msg)
        actor = User.objects.get(pk=operation.requested_by_id)
        try:
            # The savepoint rolls back a refused effect, NOT prior items or its
            # durable claim. The terminal receipt is written only after it exits.
            with transaction.atomic():
                _valid_preview(operation)
                result = execute_effect(
                    actor, BulkKind(operation.kind), selected, item.effect_key
                )
                _valid_preview(operation)
            state = (
                BulkItemState.SKIPPED_CHANGED
                if result["code"] == "proposal_invalidated"
                else BulkItemState.DONE
            )
        except (PermissionDenied, Http404):
            state, result = BulkItemState.SKIPPED_FORBIDDEN, {"code": "access_revoked"}
        except VersionConflict:
            state, result = BulkItemState.SKIPPED_CHANGED, {"code": "subject_changed"}
        except (ValidationError, IllegalTransition) as error:
            state, result = (
                BulkItemState.FAILED,
                {
                    "code": "execution_refused",
                    "detail": str(error),
                },
            )
        except (IntegrityError, IntentConflict, ValueError):
            state, result = BulkItemState.FAILED, {"code": "execution_refused"}
        item.state = state
        item.result_json = result
        item.finished_at = timezone.now()
        item.save(update_fields=["state", "result_json", "finished_at"])
        record_bulk(
            operation,
            "bulk.item_finished",
            metadata={"item_id": str(item.pk), "state": state, "fence": item.fence},
        )
        _finish(operation)


def due_operations(tenant_id: UUID) -> tuple[UUID, ...]:
    """Collect durable work inside a short tenant phase, never retaining a queryset."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        return tuple(
            BulkOperation.objects.filter(state__in=ACTIVE_OPERATIONS)
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)
        )

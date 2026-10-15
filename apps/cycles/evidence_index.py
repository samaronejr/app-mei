"""Capture and read private immutable evidence generations under the cycle fence."""

from uuid import UUID

from django.db import transaction
from django.http import Http404

from apps.accounts.models import User
from apps.cycles.completion_sources import lock_index_subjects
from apps.cycles.export_records import EvidenceIndex
from apps.cycles.index_projection import collect_index, digest_data, source_digest
from apps.cycles.models import ServiceCycle
from apps.cycles.services.approvals import _lock_scope
from apps.cycles.services.cycle import (
    check_version,
    emit,
    load_cycle,
    require_actor,
)

EVIDENCE_EXPORT = "evidence.export"


def index_current(cycle: ServiceCycle) -> bool:
    """Compare the latest persisted generation; absent indexes need no projection."""
    index = (
        EvidenceIndex.objects.filter(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            amendment=cycle.amendment,
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    return index is not None and index.source_digest == source_digest(cycle)


def freeze_index(actor: User, cycle: ServiceCycle) -> EvidenceIndex:
    """Freeze a cut under the caller's authorization and aggregate fences."""
    lock_index_subjects(cycle)
    data = collect_index(cycle)
    index = EvidenceIndex(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        amendment=cycle.amendment,
        cycle_revision=cycle.version,
        source_digest=source_digest(cycle),
        created_by=actor,
    )
    data["generation"] = {
        "id": str(index.pk),
        "generated_at": index.created_at.isoformat(),
        "amendment": index.amendment,
        "cycle_revision": index.cycle_revision,
    }
    index.data = data
    index.digest = digest_data(data)
    index.save(force_insert=True)
    return index


@transaction.atomic
def capture_index(
    actor: User, cycle_id: UUID, *, expected_version: int
) -> EvidenceIndex:
    """Capture the rendered cycle revision and audit the new immutable index."""
    candidate = load_cycle(actor, cycle_id)
    scope = _lock_scope(actor, candidate, {actor.pk})
    cycle = scope.cycle
    check_version(cycle.version, expected_version)
    scope.require(EVIDENCE_EXPORT)
    index = freeze_index(actor, cycle)
    cycle.version += 1
    cycle.save(update_fields=["version"])
    emit(
        cycle,
        actor,
        "evidence.indexed",
        payload={"index_id": str(index.pk), "digest": index.digest},
    )
    return index


def evidence_index(
    actor: User, cycle_id: UUID, *, index_id: UUID | None = None
) -> EvidenceIndex | None:
    """Read the latest index only after client scope and capability checks."""
    cycle = load_cycle(actor, cycle_id)
    require_actor(actor, EVIDENCE_EXPORT)
    query = EvidenceIndex.objects.filter(client_id=cycle.client_id, cycle=cycle)
    if index_id is not None:
        index = query.filter(pk=index_id).first()
        if index is None:
            raise Http404
        return index
    return query.order_by("-created_at", "-pk").first()


__all__ = [
    "capture_index",
    "collect_index",
    "evidence_index",
    "freeze_index",
    "index_current",
    "source_digest",
]

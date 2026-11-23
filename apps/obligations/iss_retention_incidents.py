"""Persist and reconcile the locked fiscal head's evidence incidents."""

import hashlib
import json
from typing import TYPE_CHECKING, Final, NamedTuple

from django.db.models import Q
from django.utils import timezone

from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.integrations.authority import Problem, raise_problem
from apps.obligations.iss_retention_evidence_v1 import (
    EVIDENCE_DISAGREES,
    EVIDENCE_UNAVAILABLE,
    ISSDecision,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from apps.obligations.iss_retention_evidence_v1 import ISSOriginal
    from apps.obligations.models.fiscal import FiscalDocument, FiscalRevision

CONSISTENCY_INCIDENT: Final = "fiscal.consistency_incident"
_ISS_RECOVERY: Final = "fiscal.iss_evidence_recovered"
_ISS_RECOVERY_BLOCKED: Final = "fiscal.iss_recovery_blocked"


def iss_evidence_key(originals: "Iterable[ISSOriginal]") -> str:
    """Order-independent, bounded audit identity; no copied vault keys or XML."""
    current = sorted(
        (str(original.document_id), original.sha256 or "", original.code or "")
        for original in originals
        if original.document_id is not None
    )
    return hashlib.sha256(
        json.dumps(current, separators=(",", ":")).encode()
    ).hexdigest()


class ISSResolution(NamedTuple):
    """Result of reconciling this head, including ownership of unfinished fields."""

    state: str
    decision: ISSDecision
    derive_fields: bool = False


def incident_episode(ref: ObjectRef, revision_id: str) -> "QuerySet[Event]":
    """Use the same episode for contradiction deduplication and recovery."""
    episode = Event.objects.filter(
        object_type=ref.type,
        object_id=ref.id,
        action=CONSISTENCY_INCIDENT,
        metadata__revision_id=revision_id,
    )
    boundary = (
        Event.objects.filter(
            Q(action=_ISS_RECOVERY, object_type=ref.type, object_id=ref.id)
            | Q(
                action="fiscal.head_changed",
                object_type="FiscalRevision",
                object_id=revision_id,
            )
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    if boundary is not None:
        episode = episode.filter(
            Q(created_at__gt=boundary.created_at)
            | Q(created_at=boundary.created_at, pk__gt=boundary.pk)
        )
    return episode


def _legacy_incident_blocks_recovery(
    fiscal: "FiscalDocument", ref: ObjectRef, revision: "FiscalRevision"
) -> bool:
    events = Event.objects.filter(object_type=ref.type, object_id=ref.id)
    # A rollback writer did not name the affected revision. A head may be
    # re-promoted, but an event cannot refer to a revision not yet observed.
    unscoped = events.filter(
        action=CONSISTENCY_INCIDENT,
        created_at__gte=revision.observed_at,
    ).filter(
        Q(metadata__revision_id__isnull=True)
        | Q(metadata__revision_id=None)
        | Q(metadata__revision_id="")
    )
    if not unscoped.exists():
        return False
    metadata: dict[str, object] = {
        "revision_id": str(revision.pk),
        "reason": "legacy_incident_without_revision",
    }
    if not events.filter(action=_ISS_RECOVERY_BLOCKED, metadata=metadata).exists():
        record_event(
            action=_ISS_RECOVERY_BLOCKED,
            tenant_id=fiscal.tenant_id,
            obj=ref,
            metadata=metadata,
        )
    return True


def reconcile_iss_incident(
    fiscal: "FiscalDocument",
    revision: "FiscalRevision",
    decision: ISSDecision,
    *,
    evidence_key: str,
    derive_on_recovery: bool = False,
) -> ISSResolution:
    """Use the entire current outage, not an unrelated latest audit event."""
    state = fiscal.fiscal_state
    if state != "incerta" and decision.cause is None:
        return ISSResolution(state, decision)
    ref = ObjectRef("FiscalDocument", str(fiscal.pk))
    revision_id = str(revision.pk)
    episode = incident_episode(ref, revision_id)
    outage = episode.filter(
        metadata__cause=EVIDENCE_UNAVAILABLE, metadata__retryable=True
    )
    can_recover = (
        state == "incerta"
        and not _legacy_incident_blocks_recovery(fiscal, ref, revision)
        and outage.exists()
        and not episode.exclude(pk__in=outage.values("pk")).exists()
    )
    if decision.cause:
        if (
            state != "incerta"
            or not episode.filter(
                metadata__cause=decision.cause, metadata__evidence_key=evidence_key
            ).exists()
        ):
            raise_problem(
                fiscal.client,
                Problem(
                    "incidente_consistencia_fiscal", fiscal.pk, timezone.localdate()
                ),
            )
            record_event(
                action=CONSISTENCY_INCIDENT,
                tenant_id=fiscal.tenant_id,
                obj=ref,
                metadata={
                    "revision_id": revision_id,
                    "cause": decision.cause,
                    "evidence_key": evidence_key,
                    "retryable": decision.cause == EVIDENCE_UNAVAILABLE
                    and (state != "incerta" or can_recover),
                    "derive_fields": derive_on_recovery
                    and decision.cause == EVIDENCE_UNAVAILABLE,
                },
            )
        return ISSResolution("incerta", decision)
    if not can_recover:
        return ISSResolution(state, ISSDecision(cause=EVIDENCE_DISAGREES))
    derive_fields = outage.filter(metadata__derive_fields=True).exists()
    record_event(
        action=_ISS_RECOVERY,
        tenant_id=fiscal.tenant_id,
        obj=ref,
        metadata={"revision_id": revision_id},
    )
    return ISSResolution(revision.fiscal_state, decision, derive_fields)

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


def iss_evidence_key(
    originals: "Iterable[ISSOriginal]", *, failed_key: str = ""
) -> str:
    """Order-independent, bounded audit identity; no copied vault keys or XML."""
    current = sorted(
        (str(original.document_id), original.sha256 or "", original.code or "")
        for original in originals
        if original.document_id is not None
    )
    identity = [current, failed_key] if failed_key else current
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()


class ISSOutageIdentity(NamedTuple):
    """Full snapshot identity and separate attribution to failed originals."""

    snapshot: str
    failed: str


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
        .exclude(fiscal_order__isnull=True)
        .order_by("-fiscal_order")
        .values_list("fiscal_order", flat=True)
        .first()
    )
    if boundary is not None:
        episode = episode.filter(
            Q(fiscal_order__gt=boundary) | Q(fiscal_order__isnull=True)
        )
    return episode


def _revision_registration_order(
    fiscal: "FiscalDocument", revision: "FiscalRevision"
) -> int | None:
    # Both old and current writers log this link in the same locked
    # transaction as creation. A promotion can happen much later.
    anchors = list(
        Event.objects.filter(
            action="fiscal.revision_registered",
            object_type="FiscalRevision",
            object_id=str(revision.pk),
            metadata__document_id=str(fiscal.pk),
        )
        .order_by()
        .values_list("fiscal_order", flat=True)[:2]
    )
    return anchors[0] if len(anchors) == 1 else None


def _legacy_incident_blocks_recovery(
    fiscal: "FiscalDocument", ref: ObjectRef, revision: "FiscalRevision"
) -> bool:
    events = Event.objects.filter(object_type=ref.type, object_id=ref.id)
    unscoped = events.filter(action=CONSISTENCY_INCIDENT).filter(
        Q(metadata__revision_id__isnull=True)
        | Q(metadata__revision_id=None)
        | Q(metadata__revision_id="")
    )
    if not unscoped.exists():
        return False
    registration = _revision_registration_order(fiscal, revision)
    if registration is not None:
        unscoped = unscoped.filter(
            Q(fiscal_order__isnull=True) | Q(fiscal_order__gte=registration)
        )
        if not unscoped.exists():
            return False
    unordered = (
        registration is None or unscoped.filter(fiscal_order__isnull=True).exists()
    )
    metadata: dict[str, object] = {
        "revision_id": str(revision.pk),
        "reason": (
            "legacy_incident_order_unknown"
            if unordered
            else "legacy_incident_without_revision"
        ),
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
    evidence_key: str | ISSOutageIdentity,
    derive_on_recovery: bool = False,
) -> ISSResolution:
    """Use the entire current outage, not an unrelated latest audit event."""
    state = fiscal.fiscal_state
    if state != "incerta" and decision.cause is None:
        return ISSResolution(state, decision)
    failed_evidence_key = ""
    if isinstance(evidence_key, ISSOutageIdentity):
        evidence_key, failed_evidence_key = evidence_key
    ref = ObjectRef("FiscalDocument", str(fiscal.pk))
    revision_id = str(revision.pk)
    episode = incident_episode(ref, revision_id)
    outage = episode.filter(
        fiscal_order__isnull=False,
        metadata__cause=EVIDENCE_UNAVAILABLE,
        metadata__retryable=True,
    )
    can_recover = (
        state == "incerta"
        and not _legacy_incident_blocks_recovery(fiscal, ref, revision)
        and outage.exists()
        and not episode.exclude(pk__in=outage.values("pk")).exists()
    )
    if decision.cause:
        peers = episode.filter(metadata__cause=decision.cause)
        if decision.cause == EVIDENCE_UNAVAILABLE:
            last_key = (
                peers.filter(fiscal_order__isnull=False)
                .order_by("-fiscal_order")
                .values_list("metadata__evidence_key", flat=True)
                .first()
            )
            duplicate = last_key == evidence_key
        else:
            duplicate = peers.filter(metadata__evidence_key=evidence_key).exists()
        if state != "incerta" or not duplicate:
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
                    **(
                        {"failed_evidence_key": failed_evidence_key}
                        if failed_evidence_key
                        else {}
                    ),
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

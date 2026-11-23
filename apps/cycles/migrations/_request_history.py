"""Frozen AXRM-049 cutover contract, based on supported pre-upgrade writers.

An out-of-band admin/raw-SQL flip and reversal with no event or surviving
artifact is indistinguishable from an ordinary history and is out of scope.
No display string, current waiver, or general federal-provider origin is proof.
"""

from typing import NamedTuple, TypedDict
from uuid import UUID

INTERNAL = "interno"
DRAFT = "rascunho"
SENT = "solicitado"
ACCEPTED = "aceito"
REJECTED = "rejeitado"
WAIVED = "dispensado"
KNOWN_STATES = {
    DRAFT,
    SENT,
    ACCEPTED,
    REJECTED,
    WAIVED,
    "substituido",
    "recebido_pela_equipe",
    "em_revisao",
    INTERNAL,
}
REPORT = "cycles.request_visibility_repaired"


class Request(NamedTuple):
    id: UUID
    tenant: UUID
    client: UUID
    cycle: UUID
    state: str
    version: int
    creator: UUID


class Event(NamedTuple):
    version: int
    kind: str
    data: object
    actor: UUID | None
    cycle: UUID
    schema: int


def _advance(state: str, kind: str, data: dict[str, object]) -> str | None:
    """Exactly the version-advancing commands in requests, bulk and review."""
    if kind == "request.updated":
        return state if not data and state in {DRAFT, SENT} else None
    if kind == "request.reminded":
        return state if not data and state in {SENT, REJECTED} else None
    if data.get("from") != state:
        return None
    if kind == "request.sent":
        target = SENT if state == DRAFT else None
    elif kind == "request.waived":
        target = WAIVED if state not in {ACCEPTED, WAIVED, "substituido"} else None
    elif kind == "document.reviewed" and state not in {DRAFT, WAIVED, "substituido"}:
        decision = data.get("decision")
        if decision not in (ACCEPTED, REJECTED):
            return None
        target = ACCEPTED if ACCEPTED in (decision, state) else REJECTED
    else:
        return None
    return target if target is not None and data.get("to") == target else None


def _internal_event(events: list[Event]) -> str | None:
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.kind == "request.created" and (
            data.get("origin") == "pgmei" or data.get("state") == INTERNAL
        ):
            return "internal_creation"
        if data.get("from") == INTERNAL or data.get("to") == INTERNAL:
            return "internal_transition"
        if event.kind == "federal.guide.linked":
            return "guide_link_event"
    return None


def _creation_issue(row: Request, events: list[Event]) -> str | None:
    roots = [e for e in events if e.kind == "request.created"]
    if not roots:
        return "missing_creation"
    root = roots[0]
    if (
        len(roots) != 1
        or root.version != 1
        or root.actor is None
        or root.actor != row.creator
        or root.data not in ({}, {"state": DRAFT}, {"to": DRAFT})
    ):
        return "invalid_creation"
    return None


def _replay(events: list[Event]) -> tuple[list[str], str | None]:
    states = [DRAFT]
    submissions = []
    for event in events:  # ordered by aggregate revision, not wall clock
        if event.kind == "request.created":
            continue
        if event.kind == "evidence.submitted":
            # Portal intake appends its attachment fact at the *current* revision.
            if not isinstance(event.data, dict) or event.actor is None:
                return states, "submission_mismatch"
            submissions.append(event.version)
            continue
        if event.version != len(states) + 1:
            return states, "revision_gap_or_duplicate"
        target = (
            _advance(states[-1], event.kind, event.data)
            if isinstance(event.data, dict) and event.actor is not None
            else None
        )
        if target is None:
            return states, "invalid_transition"
        states.append(target)
    if any(
        not 1 <= v <= len(states) or states[v - 1] not in (SENT, REJECTED)
        for v in submissions
    ):
        return states, "submission_mismatch"
    return states, None


def _row_issue(
    row: Request, states: list[str], snapshots: list[tuple[int, str]]
) -> str | None:
    if len(states) != row.version:
        return "revision_mismatch"
    if states[-1] != row.state:
        return "state_mismatch"
    if any(
        not 1 <= v <= len(states) or states[v - 1] != state for v, state in snapshots
    ):
        return "snapshot_mismatch"
    return None


def _reason(
    row: Request,
    events: list[Event],
    artifact: str | None,
    snapshots: list[tuple[int, str]],
) -> str | None:
    """Positive evidence wins. Only a complete, consistent ordinary chain passes."""
    signal = _internal_event(events) or artifact
    if signal:
        return signal
    if any(state == INTERNAL for _, state in snapshots):
        return "internal_snapshot"
    if any(e.cycle != row.cycle or e.schema != 1 for e in events):
        return "event_scope_or_schema"
    root = _creation_issue(row, events)
    if root:
        return root
    states, issue = _replay(events)
    return issue or _row_issue(row, states, snapshots)


class GuideArtifacts(NamedTuple):
    """Scoped evidence: a federal document receipted to SERPRO SP-DAS or an
    official_api guide_issued observation on this link, not any federal/staff guide.
    """

    system_das_receipt: bool = False
    official_guide_observation: bool = False

    def reason(self) -> str | None:
        if self.system_das_receipt:
            return "system_das_artifact"
        if self.official_guide_observation:
            return "official_guide_observation"
        return None


class AuditRecord(TypedDict):
    """Private audit insert data, not an ORM row or a visibility grant."""

    tenant_id: UUID
    actor_id: None
    action: str
    object_type: str
    object_id: str
    metadata: dict[str, str | int]


class Decision(NamedTuple):
    restore: bool
    audit: AuditRecord


def classify(
    row: Request,
    events: list[Event],
    artifacts: GuideArtifacts | None,
    snapshots: list[tuple[int, str]],
    *,
    unreadable_snapshot: bool = False,
) -> Decision | None:
    """Return the required repair and audit record, or None to leave the row alone."""
    reason = _reason(row, events, artifacts.reason() if artifacts else None, snapshots)
    if reason is None:
        if not unreadable_snapshot:
            return None
        reason = "invalid_snapshot"
    return Decision(
        restore=row.state != INTERNAL,
        audit={
            "tenant_id": row.tenant,
            "actor_id": None,
            "action": REPORT,
            "object_type": "EvidenceRequest",
            "object_id": str(row.id),
            "metadata": {
                "client_id": str(row.client),
                "cycle_id": str(row.cycle),
                "reason": reason,
                "previous_version": row.version,
                "previous_state": row.state if row.state in KNOWN_STATES else "unknown",
            },
        },
    )

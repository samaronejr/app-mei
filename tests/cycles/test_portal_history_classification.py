"""The legacy cutover replays supported producers, not request titles."""

from uuid import UUID

import pytest

from apps.cycles.migrations import _request_history as history

ACTOR, TENANT, CLIENT, CYCLE, REQUEST = (UUID(int=i) for i in range(1, 6))


def event(version: int, kind: str, **data: object) -> history.Event:
    return history.Event(version, kind, data, ACTOR, CYCLE, 1)


CREATED = event(1, "request.created")
SENT = event(2, "request.sent", **{"from": "rascunho", "to": "solicitado"})
SUBMISSION = event(2, "evidence.submitted", link_id=str(UUID(int=6)))
REVIEW = event(
    3,
    "document.reviewed",
    decision="aceito",
    **{"from": "solicitado", "to": "aceito"},
)
ROW = history.Request(REQUEST, TENANT, CLIENT, CYCLE, "solicitado", 2, ACTOR)


@pytest.mark.parametrize(
    ("events", "state", "version", "reason"),
    [
        ([CREATED, SENT], "solicitado", 2, None),
        ([CREATED, SENT, SUBMISSION, REVIEW], "aceito", 3, None),
        ([CREATED, SENT, event(3, "request.reminded")], "solicitado", 3, None),
        ([CREATED, event(2, "request.updated")], "rascunho", 2, None),
        (
            [
                CREATED,
                SENT,
                event(
                    3, "request.waived", **{"from": "solicitado", "to": "dispensado"}
                ),
            ],
            "dispensado",
            3,
            None,
        ),
        ([SENT], "solicitado", 2, "missing_creation"),
        ([CREATED, CREATED, SENT], "solicitado", 2, "invalid_creation"),
        ([CREATED._replace(actor=CLIENT), SENT], "solicitado", 2, "invalid_creation"),
        (
            [CREATED, SENT._replace(version=3)],
            "solicitado",
            3,
            "revision_gap_or_duplicate",
        ),
        ([CREATED, SENT, SENT], "solicitado", 2, "revision_gap_or_duplicate"),
        (
            [CREATED, SENT._replace(cycle=CLIENT)],
            "solicitado",
            2,
            "event_scope_or_schema",
        ),
        ([CREATED, SENT._replace(schema=2)], "solicitado", 2, "event_scope_or_schema"),
        ([CREATED, SENT], "solicitado", 3, "revision_mismatch"),
        ([CREATED, SENT], "rascunho", 2, "state_mismatch"),
        (
            [CREATED, SENT, SUBMISSION._replace(version=1)],
            "solicitado",
            2,
            "submission_mismatch",
        ),
        (
            [
                CREATED,
                event(2, "request.sent", **{"from": "aceito", "to": "solicitado"}),
            ],
            "solicitado",
            2,
            "invalid_transition",
        ),
        (
            [
                CREATED,
                SENT,
                REVIEW._replace(
                    data={"from": "solicitado", "to": "aceito", "decision": "rejeitado"}
                ),
            ],
            "aceito",
            3,
            "invalid_transition",
        ),
        (
            [CREATED, SENT, event(3, "unknown.writer")],
            "solicitado",
            3,
            "invalid_transition",
        ),
    ],
    ids=[
        "ordinary-send",
        "attachment-review",
        "reminder",
        "edit",
        "ordinary-waiver",
        "missing-creation",
        "duplicate-creation",
        "wrong-creator",
        "gap",
        "duplicate-version",
        "wrong-cycle",
        "unknown-schema",
        "row-version",
        "row-state",
        "attachment-version",
        "transition-from",
        "review-outcome",
        "unknown-writer",
    ],
)
def test_reconciliation_requires_a_complete_state_and_revision_chain(
    events: list[history.Event], state: str, version: int, reason: str | None
) -> None:
    row = ROW._replace(state=state, version=version)
    assert history._reason(row, events, None, []) == reason


@pytest.mark.parametrize(
    ("signal", "reason"),
    [
        (event(1, "request.created", origin="pgmei"), "internal_creation"),
        (event(1, "request.created", state="interno"), "internal_creation"),
        (
            event(1, "other.transition", **{"from": "interno", "to": "dispensado"}),
            "internal_transition",
        ),
        (event(2, "other.transition", to="interno"), "internal_transition"),
        (event(2, "federal.guide.linked"), "guide_link_event"),
    ],
    ids=[
        "pgmei-created",
        "internal-created",
        "internal-from",
        "internal-to",
        "guide-linked",
    ],
)
def test_any_internal_event_wins_even_over_an_ordinary_looking_row(
    signal: history.Event, reason: str
) -> None:
    assert history._reason(ROW, [CREATED, SENT, signal], None, []) == reason


def test_a_frozen_snapshot_is_evidence_not_permission_to_publish() -> None:
    chain = [CREATED, SENT]
    assert (
        history._reason(ROW, chain, None, [(1, "rascunho"), (2, "solicitado")]) is None
    )
    assert history._reason(ROW, chain, None, [(1, "interno")]) == "internal_snapshot"
    assert history._reason(ROW, chain, None, [(2, "aceito")]) == "snapshot_mismatch"


@pytest.mark.parametrize(
    ("artifacts", "reason"),
    [
        (history.GuideArtifacts(system_das_receipt=True), "system_das_artifact"),
        (
            history.GuideArtifacts(official_guide_observation=True),
            "official_guide_observation",
        ),
        (
            history.GuideArtifacts(
                system_das_receipt=True, official_guide_observation=True
            ),
            "system_das_artifact",
        ),
    ],
    ids=["das-receipt", "official-linked-issuance", "both-markers"],
)
def test_structural_guide_evidence_precedes_an_ordinary_history(
    artifacts: history.GuideArtifacts,
    reason: str,
) -> None:
    decision = history.classify(
        ROW, [CREATED, SENT], artifacts, [], unreadable_snapshot=True
    )
    assert decision is not None
    assert decision.restore
    assert decision.audit["metadata"]["reason"] == reason


def test_only_unresolved_history_or_a_positive_marker_produces_a_decision() -> None:
    assert history.classify(ROW, [CREATED, SENT], history.GuideArtifacts(), []) is None
    unreadable = history.classify(
        ROW, [CREATED, SENT], None, [], unreadable_snapshot=True
    )
    assert unreadable is not None
    assert unreadable.restore
    assert unreadable.audit["metadata"]["reason"] == "invalid_snapshot"
    internal = ROW._replace(state="interno", version=1)
    known = history.classify(
        internal, [event(1, "request.created", origin="pgmei")], None, []
    )
    assert known is not None
    assert not known.restore
    assert known.audit["metadata"] == {
        "client_id": str(CLIENT),
        "cycle_id": str(CYCLE),
        "reason": "internal_creation",
        "previous_version": 1,
        "previous_state": "interno",
    }

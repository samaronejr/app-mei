"""Synthetic exact-batch graphs shared by AXRM-022's acceptance and QA."""

from __future__ import annotations

from apps.cycles.effects.manifest import ActionKind


def manifestation_kind() -> ActionKind:
    """Make a missing action fail on its contract, rather than during collection."""
    assert "fiscal_manifestation" in {kind.value for kind in ActionKind}, (
        "AXRM-022 must register fiscal_manifestation inside manifest V1."
    )
    return ActionKind("fiscal_manifestation")

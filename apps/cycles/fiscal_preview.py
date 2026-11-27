"""Select the displayed cycle and decision entirely from a captured document."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from django.http import Http404

from apps.cycles.fiscal_records import DECISIONS
from apps.cycles.records import CycleState
from apps.cycles.services.cycle import OPEN_STATES

if TYPE_CHECKING:
    from uuid import UUID

    from django_stubs_ext import WithAnnotations

    from apps.cycles.fiscal_snapshot import CycleRow, PreviewFacts
    from apps.obligations.models import FiscalDocument


def _cycle(row: CycleRow) -> dict[str, object]:
    return {
        **row,
        "period_start": date.fromisoformat(row["period_start"]),
        "state_label": CycleState(row["state"]).label,
    }


def preview_context(
    fiscal: WithAnnotations[FiscalDocument, PreviewFacts], requested: UUID | None
) -> dict[str, object]:
    """Compare only the terminal review to the material seen in the same SELECT."""
    history = fiscal.review_history
    replaced = {row["supersedes_id"] for row in history}
    heads = {
        row["cycle_id"]: row
        for row in history
        if row["revision_id"] == str(fiscal.current_revision_id)
        and row["pk"] not in replaced
    }
    cycles = fiscal.review_cycles
    eligible = [row for row in cycles if row["eligible"]]
    if requested is None:
        pending = (
            row
            for row in eligible
            if row["state"] in OPEN_STATES
            and (
                row["pk"] not in heads
                or heads[row["pk"]]["input_digest"] != fiscal.review_material
            )
        )
        selected = next(pending, next(iter(eligible or cycles), None))
    else:
        selected = next((row for row in cycles if row["pk"] == str(requested)), None)
        if selected is None:
            raise Http404
    active = heads.get(selected["pk"]) if selected and selected["eligible"] else None
    labels = dict(DECISIONS)
    return {
        "cycles": [_cycle(c) for c in cycles],
        "cycle": _cycle(selected) if selected else None,
        "review_eligible": bool(selected and selected["eligible"]),
        "review_open": bool(
            selected and selected["eligible"] and selected["state"] in OPEN_STATES
        ),
        "head": {**active, "decision_label": labels[active["decision"]]}
        if active
        else None,
        "head_current": bool(
            active and active["input_digest"] == fiscal.review_material
        ),
        "reviews": [
            {
                **row,
                "reviewed_at": datetime.fromisoformat(row["reviewed_at"]),
                "decision_label": labels[row["decision"]],
            }
            for row in history
            if selected and row["cycle_id"] == selected["pk"]
        ],
        "revisions": [
            {
                **row,
                "observed_at": datetime.fromisoformat(row["observed_at"]),
                "source_effective_at": datetime.fromisoformat(
                    row["source_effective_at"]
                )
                if row["source_effective_at"]
                else None,
            }
            for row in fiscal.revision_rows
        ],
    }

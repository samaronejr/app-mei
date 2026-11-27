"""Cycle selection and historical display from directly projected fiscal facts."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import RequestFactory

from apps.core.tenancy import tenant_context
from apps.cycles.fiscal_cycle_input import submitted_cycle
from apps.cycles.fiscal_preview import preview_context
from apps.cycles.fiscal_snapshot import load_snapshot
from tests.cycles.fiscal_review_fixtures import other_cycle
from tests.cycles.test_fiscal_document_review import world
from tests.cycles.test_fiscal_reader_projections import (
    move_reviewed_document,
    projected_fact,
)

if TYPE_CHECKING:
    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_projection_keeps_old_cycle_history_separate_from_current_revision(
    world: CaptureWorld,
) -> None:
    moved, september, october, decisions = move_reviewed_document(world)
    with tenant_context(world.tenant_id):
        snapshot = load_snapshot(moved.pk)
        old, new = (
            preview_context(snapshot, cycle.pk) for cycle in (september, october)
        )
    assert not old["review_eligible"]
    assert {row["pk"] for row in cast("list[dict[str, object]]", old["reviews"])} == {
        str(row.pk) for row, _ in decisions
    }
    assert new["review_eligible"]
    assert new["reviews"] == []
    assert new["head"] is None
    pending = projected_fact(world, moved)
    assert pending is not None
    assert pending.target_id == october.pk


AMBIGUOUS_CYCLES = pytest.mark.parametrize(
    ("method", "query", "body", "status"),
    [
        ("get", "BA", "", 422),
        ("get", "AA", "", 422),
        ("get", "B", "A", 422),
        ("get", "A", "AA", 422),
        ("post", "BA", "A", 422),
        ("post", "AA", "A", 422),
        ("post", "A", "BA", 422),
        ("post", "A", "AA", 422),
        ("post", "B", "A", 404),
    ],
)


@AMBIGUOUS_CYCLES
def test_dormant_selector_refuses_to_retarget_a_projected_cycle(
    world: CaptureWorld, method: str, query: str, body: str, status: int
) -> None:
    fiscal, first, second = world.ingest(), world.setup.cycle, other_cycle(world)
    with tenant_context(world.tenant_id):
        keyed = {r["pk"] for r in load_snapshot(fiscal.pk).review_cycles}
    cycles = {"A": str(first.pk), "B": str(second.pk)}
    assert set(cycles.values()) == keyed
    path = f"/?{urlencode([('ciclo', cycles[c]) for c in query])}"
    factory = RequestFactory()
    request = (
        factory.generic(
            "GET",
            path,
            urlencode([("cycle_id", cycles[c]) for c in body]),
            content_type="application/x-www-form-urlencoded",
        )
        if method == "get"
        else factory.post(path, {"cycle_id": [cycles[c] for c in body]})
    )
    try:
        submitted_cycle(request)
    except Http404:
        outcome = 404
    except ValidationError:
        outcome = 422
    else:
        outcome = 200
    assert outcome == status
    assert submitted_cycle(factory.get("/", {"ciclo": str(first.pk)})) == first.pk

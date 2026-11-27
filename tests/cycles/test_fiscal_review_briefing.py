"""Real feedback must be scoped to the complete fiscal pending fact."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.utils import timezone

from tests.cycles.fiscal_review_fixtures import other_cycle
from tests.cycles.test_fiscal_document_review import world
from tests.cycles.test_fiscal_reader_projections import (
    advance_pending_fact,
    projected_fact,
    record_in,
)
from tests.fakes.focus import FakeFocus
from tests.integrations.capture_fixtures import START, capture_api, page

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)


def poll_forward(
    world: CaptureWorld, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = world.stream()
    with FakeFocus((page([]), page([]))) as fake:
        settings.FOCUS_FAKE_ORIGIN = fake.origin
        capture_api().synchronize(world.tenant_id, stream.pk)
        monkeypatch.setattr(timezone, "now", lambda: START + timedelta(hours=1))
        capture_api().synchronize(world.tenant_id, stream.pk)
    assert world.reload(stream).last_api_success_at == START + timedelta(hours=1)


def test_poll_only_replay_preserves_the_projected_row(
    world: CaptureWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fiscal = world.ingest()
    before = projected_fact(world, fiscal)
    assert before is not None
    poll_forward(world, settings, monkeypatch)
    assert projected_fact(world, fiscal) == before


@pytest.mark.parametrize("change", ["cycle", "material-only", "review-then-cce"])
def test_projected_fact_tracks_a_pending_cycle_without_a_revision_bump(
    world: CaptureWorld, settings: SettingsWrapper, change: str
) -> None:
    fiscal, first = world.ingest(), world.setup.cycle
    second = first if change == "material-only" else other_cycle(world)
    head = record_in(world, fiscal, first) if change == "review-then-cce" else None
    initial = projected_fact(world, fiscal)
    assert initial is not None
    assert initial.target_id == (second if head else first).pk
    advance_pending_fact(world, fiscal, second, change, settings)
    pending = projected_fact(world, fiscal)
    assert pending is not None
    assert pending.target_id == second.pk
    assert pending.fact_version != initial.fact_version

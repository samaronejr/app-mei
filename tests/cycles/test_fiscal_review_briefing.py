"""Real feedback must be scoped to the complete fiscal pending fact."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.models import BriefingFeedback
from tests.cycles.fiscal_review_fixtures import other_cycle
from tests.cycles.fiscal_review_reader_fixtures import (
    dismissal_form,
    fiscal_item,
    hoje_browser,
    hrefs,
    review_form,
    review_path,
)
from tests.cycles.test_fiscal_document_review import world
from tests.cycles.test_fiscal_reader_projections import (
    advance_pending_fact,
    projected_fact,
    record_in,
    replace_stale_head,
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


@pytest.mark.parametrize("dismissed", [False, True], ids=["old-form", "stored"])
@pytest.mark.parametrize("change", ["cycle", "material-only", "review-then-cce"])
def test_hoje_feedback_expires_only_for_a_different_pending_fact(
    world: CaptureWorld,
    settings: SettingsWrapper,
    change: str,
    dismissed: bool,
) -> None:
    fiscal, first = world.ingest(), world.setup.cycle
    second = first if change == "material-only" else other_cycle(world)
    first_head = (
        record_in(world, fiscal, first) if change == "review-then-cce" else None
    )
    before = second if first_head else first
    browser = hoje_browser(world)
    dashboard, dismiss = reverse("dashboard"), reverse("briefing-dismiss")
    initial = browser.get(dashboard)
    item = fiscal_item(world, fiscal)
    assert item is not None
    form = dismissal_form(initial.content, fiscal)
    assert form["fact_version"] == item.fact_version
    if dismissed:
        assert browser.post(dismiss, form).status_code == 302
        assert not hrefs(browser.get(dashboard).content, fiscal)
    old_b = advance_pending_fact(world, fiscal, second, change, settings)
    if not dismissed:
        assert browser.post(dismiss, form).status_code == 409
    updated = browser.get(dashboard)
    current = fiscal_item(world, fiscal)
    assert current is not None
    assert hrefs(updated.content, fiscal) == [current.url]
    assert hrefs(initial.content, fiscal) == [review_path(fiscal, before)] == [item.url]
    assert current.url == review_path(fiscal, second)
    assert (item.id == current.id) == (change != "cycle")
    fresh = dismissal_form(updated.content, fiscal)
    assert fresh["fact_version"] == current.fact_version != form["fact_version"]
    preview = browser.get(current.url)
    assert review_form(preview.content)["expected_head_id"] == (
        str(old_b.pk) if old_b else ""
    )
    with tenant_context(world.tenant_id):
        assert BriefingFeedback.objects.filter(subject_ref=fiscal.pk).count() == int(
            dismissed
        )
    assert browser.post(dismiss, fresh).status_code == 302
    assert not hrefs(browser.get(dashboard).content, fiscal)


def test_review_alone_only_clears_its_gap_and_later_head_is_part_of_the_fact(
    world: CaptureWorld,
) -> None:
    fiscal, first, second = world.ingest(), world.setup.cycle, other_cycle(world)
    first_root = record_in(world, fiscal, first)
    original, pending, root, correction = replace_stale_head(
        world, fiscal, second, first_root, lambda: fiscal_item(world, fiscal)
    )
    assert pending.url == original.url
    form = review_form(world.setup.firm().get(pending.url).content)
    assert form["expected_head_id"] == str(correction.pk)
    assert form["input_digest"] == root.input_digest
    assert pending.fact_version != original.fact_version


def test_poll_only_replay_does_not_expire_hoje_feedback(
    world: CaptureWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fiscal, browser = world.ingest(), hoje_browser(world)
    dashboard = reverse("dashboard")
    first = browser.get(dashboard)
    item = fiscal_item(world, fiscal)
    assert item is not None
    assert hrefs(first.content, fiscal) == [item.url]
    form = dismissal_form(first.content, fiscal)
    assert browser.post(reverse("briefing-dismiss"), form).status_code == 302
    poll_forward(world, settings, monkeypatch)
    assert fiscal_item(world, fiscal) == item
    assert not hrefs(browser.get(dashboard).content, fiscal)

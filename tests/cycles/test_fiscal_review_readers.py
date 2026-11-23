"""Review through the cycle-bound readers, including ambiguous input and feedback."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.records import CycleEvent
from tests.cycles.fiscal_review_fixtures import blank_review_notes, other_cycle
from tests.cycles.fiscal_review_race_fixtures import review_race
from tests.cycles.fiscal_review_reader_fixtures import (
    change_state,
    fiscal_item,
    hoje_browser,
    hrefs,
    record_in,
    retain_same_revision_cce,
    review_action,
    review_form,
    review_path,
    seed_legacy_decision,
)
from tests.cycles.test_fiscal_document_review import world
from tests.cycles.test_fiscal_review_writer import legacy_digest
from tests.cycles.test_playbook_http_conflicts import HiddenInputs
from tests.integrations.capture_fixtures import MONTH, xml
from tests.obligations.fiscal_fixtures import COUNTERPART

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("gap", ["later", "earlier", "stale", "nfse"])
def test_all_readers_and_links_follow_only_the_acting_cycle(
    world: CaptureWorld,
    gap: str,
) -> None:
    fiscal, first, second = world.ingest(), world.setup.cycle, other_cycle(world)
    if gap == "nfse":
        with tenant_context(world.tenant_id):
            type(fiscal).objects.filter(pk=fiscal.pk).update(
                family="nfse_nacional",
                competence=MONTH,
                issue_date=MONTH + timedelta(days=30),
            )
    current, pending = (second, first) if gap == "earlier" else (first, second)
    old = record_in(world, fiscal, pending) if gap == "stale" else None
    if old:
        change_state(world, fiscal, "cancelada")
    head = record_in(world, fiscal, current)
    browser = hoje_browser(world)
    queue = reverse("document-queue") + "?fila=fiscais"
    path, reviewed_path = review_path(fiscal, pending), review_path(fiscal, current)
    item = fiscal_item(world, fiscal)
    assert item is not None
    pending_detail = reverse("cycle-detail", args=[pending.pk])
    for entry in (
        queue,
        reverse("dashboard"),
        pending_detail,
    ):
        assert hrefs(browser.get(entry).content, fiscal) == [path]
    assert b'data-review-decision=""' in browser.get(pending_detail).content
    assert item.url == path
    reviewed = browser.get(reverse("cycle-detail", args=[current.pk]))
    assert b'data-review-decision="rejeitado"' in reviewed.content
    assert hrefs(reviewed.content, fiscal) == [reviewed_path]
    preview = browser.get(item.url)
    fields = review_form(preview.content)
    assert fields["cycle_id"] == str(pending.pk)
    assert fields["expected_version"] == str(pending.version)
    assert fields["input_digest"] == head.input_digest
    assert fields["revision_id"] == str(fiscal.current_revision_id)
    assert fields["expected_head_id"] == (str(old.pk) if old else "")
    assert f'href="?ciclo={current.pk}"'.encode() in preview.content
    assert (
        browser.post(path, {**fields, "expected_head_id": str(head.pk)}).status_code
        == 409
    )
    assert browser.post(path, fields).status_code == 302
    assert not hrefs(browser.get(queue).content, fiscal)
    with tenant_context(world.tenant_id):
        new = FiscalDocumentReview.objects.get(cycle=pending, decision="aceito")
    assert new.supersedes_id == (old.pk if old else None)
    assert b'data-review-decision="aceito"' in browser.get(pending_detail).content
    assert review_form(browser.get(reviewed_path).content)["expected_head_id"] == str(
        head.pk
    )


@pytest.mark.parametrize("surface", ["document-queue", "dashboard"])
def test_captured_discrepancy_links_only_to_its_read_only_workspace(
    world: CaptureWorld,
    surface: str,
) -> None:
    with patch(
        "tests.integrations.capture_fixtures.xml",
        return_value=xml(recipient=COUNTERPART),
    ):
        fiscal = world.ingest()
    cycle, browser = world.setup.cycle, hoje_browser(world)
    assert fiscal.role_verification == "divergent"
    assert fiscal.issue_date is not None
    assert cycle.period_start <= fiscal.issue_date <= cycle.period_end
    (bare,) = hrefs(browser.get(reverse(surface), {"fila": "fiscais"}).content, fiscal)
    response = browser.get(bare)
    assert response.status_code == 200
    assert bare == reverse("document-review", args=[fiscal.pk])
    assert "cycle_id" not in HiddenInputs(response.content.decode()).values
    assert not hrefs(
        browser.get(reverse("cycle-detail", args=[cycle.pk])).content, fiscal
    )
    assert browser.get(review_path(fiscal, cycle)).status_code == 404


@pytest.mark.parametrize(
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
def test_workspace_refuses_ambiguous_cycle_inputs(
    world: CaptureWorld,
    method: str,
    query: str,
    body: str,
    status: int,
) -> None:
    fiscal, first, second = world.ingest(), world.setup.cycle, other_cycle(world)
    cycles, browser = {"A": str(first.pk), "B": str(second.pk)}, world.setup.firm()
    bare, canonical = (
        reverse("document-review", args=[fiscal.pk]),
        review_path(fiscal, first),
    )
    fields = review_form(browser.get(canonical).content)
    path = f"{bare}?{urlencode([('ciclo', cycles[c]) for c in query])}"
    if method == "get":
        response = browser.generic(
            "GET",
            path,
            urlencode([("cycle_id", cycles[c]) for c in body]),
            content_type="application/x-www-form-urlencoded",
        )
    else:
        response = browser.post(path, {**fields, "cycle_id": [cycles[c] for c in body]})
    assert response.status_code == status
    assert "cycle_id" not in HiddenInputs(response.content.decode()).values
    with tenant_context(world.tenant_id):
        assert not CycleEvent.objects.filter(type="fiscal.document.reviewed").exists()
    assert browser.post(canonical, fields).status_code == 302


@pytest.mark.parametrize("value", [None, "not-a-uuid", "unknown"])
def test_a_bare_untrusted_post_never_falls_back_to_a_writable_form(
    world: CaptureWorld,
    value: str | None,
) -> None:
    fiscal, first, second = world.ingest(), world.setup.cycle, other_cycle(world)
    record_in(world, fiscal, first)
    browser = world.setup.firm()
    bare = reverse("document-review", args=[fiscal.pk])
    rendered = browser.get(bare)
    assert review_action(bare, rendered.content) == review_path(fiscal, second)
    fields = review_form(rendered.content)
    assert browser.post(review_path(fiscal, second), fields).status_code == 302
    assert review_form(browser.get(bare).content)["cycle_id"] == str(first.pk)
    del fields["cycle_id"]
    if value is not None:
        fields["cycle_id"] = str(uuid4()) if value == "unknown" else value
    response = browser.post(bare, fields)
    assert response.status_code == (404 if value == "unknown" else 422)
    assert "cycle_id" not in HiddenInputs(response.content.decode()).values
    with tenant_context(world.tenant_id):
        assert FiscalDocumentReview.objects.count() == 2
        assert CycleEvent.objects.filter(type="fiscal.document.reviewed").count() == 2


def test_period_move_keeps_exact_revision_history_but_no_old_form(
    world: CaptureWorld,
) -> None:
    first_cycle, fiscal = world.setup.cycle, world.ingest()
    root = record_in(world, fiscal, first_cycle)
    correction = record_in(world, fiscal, first_cycle, "aceito", root)
    newer = world.ingest(version=2)
    second_root = record_in(world, newer, first_cycle, "requer_xml")
    october = other_cycle(world, start=date(2026, 10, 1))
    with patch(
        "tests.integrations.capture_fixtures.xml",
        return_value=xml().replace(b"2026-09-15T", b"2026-10-01T"),
    ):
        moved = world.ingest(version=3)
    assert moved.pk == fiscal.pk
    assert moved.issue_date == october.period_start
    browser = world.setup.firm()
    prior = browser.get(review_path(moved, first_cycle))
    assert prior.status_code == 200
    assert "cycle_id" not in HiddenInputs(prior.content.decode()).values
    for row, revision in (
        (root, fiscal.current_revision),
        (correction, fiscal.current_revision),
        (second_root, newer.current_revision),
    ):
        assert revision is not None
        history = re.search(
            rf'<li\b[^>]*data-fiscal-review-row="{row.pk}"[^>]*>(.*?)</li>',
            prior.content.decode(),
            re.DOTALL,
        )
        assert history is not None
        assert row.decision_label in history[1]
        assert row.note in history[1]
        anchor = f"fiscal-revision-{revision.pk}"
        link = re.search(rf'href="#{anchor}"[^>]*>([^<]+)</a>', history[1])
        assert link is not None
        assert revision.source_namespace in link[1]
        assert revision.provider_version in link[1]
        assert f'id="{anchor}"'.encode() in prior.content
    path = review_path(moved, october)
    new_page = browser.get(path)
    assert not new_page.context["reviews"]
    form = review_form(new_page.content)
    assert not form["expected_head_id"]
    refused = browser.post(
        review_path(moved, first_cycle),
        {
            **form,
            "cycle_id": str(first_cycle.pk),
            "expected_version": str(first_cycle.version),
        },
    )
    assert refused.status_code == 422
    assert "cycle_id" not in HiddenInputs(refused.content.decode()).values
    assert browser.post(path, form).status_code == 302
    with tenant_context(world.tenant_id):
        assert FiscalDocumentReview.objects.get(cycle=october).supersedes_id is None
        assert FiscalDocumentReview.objects.filter(cycle=first_cycle).count() == 3


@pytest.mark.parametrize("material", ["cancellation", "cce"])
def test_changed_material_refuses_old_rendered_form_and_accepts_only_a_fresh_one(
    world: CaptureWorld,
    settings: SettingsWrapper,
    material: str,
) -> None:
    cycle, fiscal = world.setup.cycle, world.ingest()
    root = record_in(world, fiscal, cycle)
    browser, path = world.setup.firm(), review_path(fiscal, cycle)
    old = review_form(browser.get(path).content)
    if material == "cce":
        retain_same_revision_cce(world, settings)
    else:
        change_state(world, fiscal, "cancelada")
    assert browser.post(path, old).status_code == 409
    queue = reverse("document-queue") + "?fila=fiscais"
    assert hrefs(browser.get(queue).content, fiscal) == [path]
    assert (
        b'data-review-decision=""'
        in browser.get(reverse("cycle-detail", args=[cycle.pk])).content
    )
    with tenant_context(world.tenant_id):
        assert list(FiscalDocumentReview.objects.values_list("pk", flat=True)) == [
            root.pk
        ]
    preview = browser.get(path)
    fresh = review_form(preview.content)
    assert {key for key in old if old[key] != fresh[key]} == {"input_digest"}
    assert (
        b'data-fiscal-artifact-role="xml_cce"'
        if material == "cce"
        else b'data-fiscal-state="cancelada"'
    ) in preview.content
    for note in blank_review_notes():
        assert browser.post(path, {**fresh, "note": note}).status_code == 422
    if material == "cce":
        second_tab = world.setup.firm()
        winner, loser = review_race(
            lambda: browser.post(path, fresh), lambda: second_tab.post(path, fresh)
        )
        assert (winner.result().status_code, loser.result().status_code) == (302, 409)
    else:
        assert browser.post(path, fresh).status_code == 302
    with tenant_context(world.tenant_id):
        assert FiscalDocumentReview.objects.count() == 2
        assert CycleEvent.objects.filter(type="fiscal.document.reviewed").count() == 2
        assert (
            FiscalDocumentReview.objects.get(supersedes=root).input_digest
            == fresh["input_digest"]
        )
    assert not hrefs(browser.get(queue).content, fiscal)
    if material == "cancellation":
        change_state(world, fiscal, "autorizada")
        assert hrefs(browser.get(queue).content, fiscal) == [path]
        assert not browser.get(path).context["head_current"]


def test_legacy_review_is_history_until_a_fresh_successor_clears_the_gap(
    world: CaptureWorld,
) -> None:
    cycle, fiscal = world.setup.cycle, world.ingest()
    revision = fiscal.current_revision_id
    assert revision is not None
    with tenant_context(world.tenant_id):
        root = seed_legacy_decision(world, fiscal, revision, legacy_digest(revision))
    browser, path = hoje_browser(world), review_path(fiscal, cycle)
    queue, detail = (
        reverse("document-queue") + "?fila=fiscais",
        reverse("cycle-detail", args=[cycle.pk]),
    )
    preview = browser.get(path)
    fields = review_form(preview.content)
    assert fields["expected_head_id"] == str(root.pk)
    assert not preview.context["head_current"]
    assert hrefs(browser.get(queue).content, fiscal) == [path]
    assert b'data-review-decision=""' in browser.get(detail).content
    item = fiscal_item(world, fiscal)
    assert item is not None
    assert item.url == path
    assert (
        browser.post(path, {**fields, "input_digest": root.input_digest}).status_code
        == 409
    )
    assert browser.post(path, fields).status_code == 302
    assert browser.get(path).context["head_current"]
    assert not hrefs(browser.get(queue).content, fiscal)
    assert b'data-review-decision="aceito"' in browser.get(detail).content
    assert fiscal_item(world, fiscal) is None

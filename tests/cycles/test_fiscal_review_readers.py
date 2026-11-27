"""Review through the cycle-bound readers, including ambiguous input and feedback."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse

from apps.authz.objects import load_visible_client
from apps.core.tenancy import tenant_context
from apps.cycles.fiscal_cycle_input import submitted_cycle
from apps.cycles.fiscal_preview import preview_context
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.fiscal_snapshot import load_snapshot
from apps.cycles.records import CycleEvent
from tests.cycles.fiscal_review_fixtures import blank_review_notes, other_cycle
from tests.cycles.fiscal_review_race_fixtures import review_race
from tests.cycles.fiscal_review_reader_fixtures import (
    fiscal_item,
    hoje_browser,
    hrefs,
    retain_same_revision_cce,
    review_action,
    review_form,
    review_path,
    seed_legacy_decision,
)
from tests.cycles.test_fiscal_document_review import world
from tests.cycles.test_fiscal_reader_projections import (
    change_state,
    cycle_gap,
    move_reviewed_document,
    projected_fact,
    record_in,
)
from tests.cycles.test_fiscal_review_writer import legacy_digest
from tests.cycles.test_fiscal_snapshot import _correct
from tests.cycles.test_playbook_http_conflicts import HiddenInputs
from tests.integrations.capture_fixtures import xml
from tests.obligations.fiscal_fixtures import COUNTERPART

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.accounts.models import User
    from apps.clients.models import ClientCompany
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


def test_workspace_does_not_mix_a_successor_with_pre_capture_material(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    fiscal, cycle = world.ingest(), world.setup.cycle
    root = record_in(world, fiscal, cycle)
    initial = (str(root.pk), root.input_digest, str(cycle.version))
    browser, path = hoje_browser(world), review_path(fiscal, cycle)

    def after_document_read(actor: User, client_id: UUID) -> ClientCompany:
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(_correct, world, settings, fiscal, root).result(timeout=30)
        return load_visible_client(actor, client_id)

    with patch("apps.cycles.fiscal_views.load_visible_client", after_document_read):
        preview = browser.get(path)
    with tenant_context(world.tenant_id):
        successor = FiscalDocumentReview.objects.get(supersedes=root)
        assert successor.revision_id == root.revision_id == fiscal.current_revision_id
        assert root.revision.artifacts.filter(role="xml_cce").exists()
        assert successor.reviewer_id == world.setup.graph.actor.pk
        assert successor.input_digest != root.input_digest
        cycle.refresh_from_db()
        updated = (str(successor.pk), successor.input_digest, str(cycle.version))
    assert preview.status_code == 200
    fields = review_form(preview.content)
    keys = ("expected_head_id", "input_digest", "expected_version")
    assert tuple(fields[k] for k in keys) == initial
    assert fields["revision_id"] == str(root.revision_id)
    assert b'data-review-freshness="current"' in preview.content
    assert f'data-fiscal-review-row="{root.pk}"'.encode() in preview.content
    assert f'data-fiscal-review-row="{successor.pk}"'.encode() not in preview.content
    assert b'data-fiscal-artifact-role="xml_cce"' not in preview.content

    refused = browser.post(path, fields)
    assert refused.status_code == 409
    fresh = review_form(refused.content)
    assert tuple(fresh[k] for k in keys) == updated
    assert f'data-fiscal-review-row="{successor.pk}"'.encode() in refused.content
    assert b'data-fiscal-artifact-role="xml_cce"' in refused.content
    assert browser.post(path, fresh).status_code == 302
    with tenant_context(world.tenant_id):
        final = FiscalDocumentReview.objects.get(supersedes=successor)
        assert final.input_digest == successor.input_digest
        assert final.reviewer_id == world.setup.graph.staff.pk
        assert CycleEvent.objects.filter(type="fiscal.document.reviewed").count() == 3


@pytest.mark.parametrize("gap", ["later", "earlier", "stale", "nfse"])
def test_all_readers_and_links_follow_only_the_acting_cycle(
    world: CaptureWorld,
    gap: str,
) -> None:
    fiscal, current, pending, old, head = cycle_gap(world, gap)
    browser = hoje_browser(world)
    queue = reverse("document-queue") + "?fila=fiscais"
    path, reviewed_path = review_path(fiscal, pending), review_path(fiscal, current)
    item = fiscal_item(world, fiscal)
    assert item is not None
    pending_detail = reverse("cycle-detail", args=[pending.pk])
    for entry in (queue, reverse("dashboard"), pending_detail):
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
    wrong_head = {**fields, "expected_head_id": str(head.pk)}
    assert browser.post(path, wrong_head).status_code == 409
    assert browser.post(path, fields).status_code == 302
    assert not hrefs(browser.get(queue).content, fiscal)
    with tenant_context(world.tenant_id):
        new = FiscalDocumentReview.objects.get(cycle=pending, decision="aceito")
    assert new.supersedes_id == (old.pk if old else None)
    chain = browser.get(path).content
    assert sorted(re.findall(rb'href="\?ciclo=([^"]+)"', chain)) == sorted(
        str(cycle.pk).encode() for cycle in (pending, current)
    )
    assert review_form(chain)["expected_head_id"] == str(new.pk)
    for row in (old, new) if old else (new,):
        assert f'data-fiscal-review-row="{row.pk}"'.encode() in chain
    assert b'data-review-decision="aceito"' in browser.get(pending_detail).content
    reviewed_form = review_form(browser.get(reviewed_path).content)
    assert reviewed_form["expected_head_id"] == str(head.pk)


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


@AMBIGUOUS_CYCLES
def test_workspace_refuses_ambiguous_cycle_inputs(
    world: CaptureWorld,
    method: str,
    query: str,
    body: str,
    status: int,
) -> None:
    fiscal, first, second = world.ingest(), world.setup.cycle, other_cycle(world)
    cycles, browser = {"A": str(first.pk), "B": str(second.pk)}, world.setup.firm()
    canonical = review_path(fiscal, first)
    bare = reverse("document-review", args=[fiscal.pk])
    fields = HiddenInputs(browser.get(canonical).content.decode()).values
    fields["decision"] = "aceito"
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
    moved, first_cycle, october, decisions = move_reviewed_document(world)
    browser = world.setup.firm()
    prior = browser.get(review_path(moved, first_cycle))
    assert prior.status_code == 200
    assert "cycle_id" not in HiddenInputs(prior.content.decode()).values
    for row, document in decisions:
        revision = document.current_revision
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
    queue = reverse("document-queue") + "?fila=fiscais"
    detail = reverse("cycle-detail", args=[cycle.pk])
    preview = browser.get(path)
    fields = review_form(preview.content)
    assert fields["expected_head_id"] == str(root.pk)
    assert not preview.context["head_current"]
    assert hrefs(browser.get(queue).content, fiscal) == [path]
    assert b'data-review-decision=""' in browser.get(detail).content
    item = fiscal_item(world, fiscal)
    assert item is not None
    assert item.url == path
    stale = browser.post(path, {**fields, "input_digest": root.input_digest})
    assert stale.status_code == 409
    assert browser.post(path, fields).status_code == 302
    assert browser.get(path).context["head_current"]
    assert not hrefs(browser.get(queue).content, fiscal)
    assert b'data-review-decision="aceito"' in browser.get(detail).content
    assert fiscal_item(world, fiscal) is None

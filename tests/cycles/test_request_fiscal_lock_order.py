"""Fiscal corrections coordinate with cycle-first child writers on the cycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceRequest, RequestInternal, RequestPurpose
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.records import CycleEvent, InternalNote
from apps.cycles.services.cycle import add_note
from apps.cycles.services.requests import (
    RequestFields,
    create_request,
    ensure_internal_request,
)
from tests.cycles.fiscal_review_fixtures import row_image
from tests.cycles.join_lock_fixtures import (
    CLIENT,
    FISCAL,
    available,
    ordered,
    paused_client,
    probe,
)
from tests.cycles.test_fiscal_document_review import world
from tests.cycles.test_fiscal_reader_projections import record_in
from tests.cycles.test_fiscal_review_writer import _prepared
from tests.portal.request_fixtures import scoped_portal_cursor

if TYPE_CHECKING:
    from uuid import UUID

    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)
PERIOD = RequestPurpose.PERIOD_FISCAL_DOCUMENTS


def test_new_period_request_and_correction_share_the_cycle_without_deadlock(
    world: CaptureWorld,
) -> None:
    fiscal, cycle = world.ingest(), world.setup.cycle
    root = record_in(world, fiscal, cycle)
    version = cycle.version
    with tenant_context(world.tenant_id):
        before = row_image(root.pk)
        assert not EvidenceRequest.objects.filter(cycle=cycle, purpose=PERIOD).exists()
        n_events = CycleEvent.objects.filter(cycle=cycle).count()
    correct = _prepared(world, fiscal, root)
    requested, reviewed = ordered(
        world.tenant_id,
        lambda: ensure_internal_request(cycle, PERIOD).pk,
        lambda: correct(decision="aceito").pk,
    )
    with tenant_context(world.tenant_id):
        cycle.refresh_from_db()
        requests = EvidenceRequest.objects.filter(cycle=cycle, purpose=PERIOD)
        reviews = FiscalDocumentReview.objects.filter(cycle=cycle, document=fiscal)
        events = CycleEvent.objects.filter(cycle=cycle)
        changes = (
            cycle.version - version,
            events.count() - n_events,
            requests.count(),
            reviews.count() - 1,
        )
        assert "40P01" not in (requested.status, reviewed.status), (
            f"SQLSTATE 40P01 is not a domain refusal: {requested=}, {reviewed=}; "
            f"committed (version, events, period requests, successors) = {changes}"
        )
        assert (requested.status, reviewed.status) == ("committed", "VersionConflict")
        assert changes == (1, 1, 1, 0)
        request = requests.get()
        assert (request.pk, request.state) == (requested.value, "interno")
        assert request.created_by_id is None
        assert RequestInternal.objects.filter(request=request).count() == 1
        assert ensure_internal_request(cycle, PERIOD).pk == request.pk
        created = events.get(aggregate_id=request.pk, type="request.created")
        assert created.actor_id is None
        assert created.payload == {"purpose": PERIOD}
        successor = correct(expected_version=cycle.version, decision="aceito")
        assert successor.supersedes_id == root.pk
        assert successor.revision_id == root.revision_id
        assert successor.input_digest == root.input_digest
        assert successor.decision == "aceito"
        assert row_image(root.pk) == before
        saved = events.get(aggregate_id=successor.pk, type="fiscal.document.reviewed")
        assert saved.payload["review_id"] == str(successor.pk)
        cycle.refresh_from_db()
        assert (cycle.version, events.count(), reviews.count()) == (
            version + 2,
            n_events + 2,
            2,
        )
    with scoped_portal_cursor(world.tenant_id, cycle.client_id) as cur:
        cur.execute(
            "SELECT id FROM cycles_evidencerequest WHERE id IN (%s, %s)",
            [request.pk, world.setup.request.pk],
        )
        assert cur.fetchall() == [(world.setup.request.pk,)]


@pytest.mark.parametrize("kind", ["request", "note"])
def test_child_write_precedes_fiscal_correction(world: CaptureWorld, kind: str) -> None:
    fiscal, cycle, actor = world.ingest(), world.setup.cycle, world.setup.graph.actor
    root = record_in(world, fiscal, cycle)
    correct = _prepared(world, fiscal, root)
    version, title = cycle.version, "Documento da competência"

    def child() -> UUID:
        if kind == "note":
            return add_note(actor, cycle.pk, title, expected_version=version).pk
        return create_request(
            actor, cycle.pk, RequestFields(title), expected_version=version
        ).pk

    left, right = ordered(world.tenant_id, child, lambda: correct().pk)
    assert (left.status, right.status) == ("committed", "VersionConflict"), (
        left,
        right,
    )
    with tenant_context(world.tenant_id):
        cycle.refresh_from_db()
        assert cycle.version == version + 1
        if kind == "note":
            assert InternalNote.objects.get(body=title, cycle=cycle).pk == left.value
        else:
            assert (
                EvidenceRequest.objects.get(title=title, cycle=cycle).pk == left.value
            )
        assert not FiscalDocumentReview.objects.filter(supersedes=root).exists()
        assert correct(expected_version=cycle.version).supersedes_id == root.pk


def test_fiscal_client_fence_allows_key_share_only(world: CaptureWorld) -> None:
    fiscal, cycle = world.ingest(), world.setup.cycle
    correct = _prepared(world, fiscal, record_in(world, fiscal, cycle))

    def check() -> None:
        modes = (
            available(CLIENT, cycle.client_id, "KEY SHARE"),
            available(CLIENT, cycle.client_id, "NO KEY UPDATE"),
        )
        assert modes == (True, False), f"fiscal client lock matrix: {modes}"

    with paused_client(world.tenant_id, lambda: correct().pk, FISCAL):
        probe(world.tenant_id, check)


def test_same_client_fence_excludes_material_writer_only(world: CaptureWorld) -> None:
    fiscal, cycle = world.ingest(), world.setup.cycle
    root = record_in(world, fiscal, cycle)
    correct = _prepared(world, fiscal, root)

    def check() -> None:
        other = available(CLIENT, world.setup.other_client.pk, "NO KEY UPDATE")
        same = available(CLIENT, cycle.client_id, "NO KEY UPDATE")
        assert (other, same) == (True, False), f"material fence: {other=}, {same=}"

    with paused_client(world.tenant_id, lambda: correct().pk, FISCAL):
        probe(world.tenant_id, check)
    with tenant_context(world.tenant_id):
        successor = FiscalDocumentReview.objects.get(supersedes=root)
        assert successor.input_digest == root.input_digest

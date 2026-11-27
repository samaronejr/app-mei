"""Evaluate the fiscal queries directly, independently of routing and templates."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from apps.briefing.fiscal_source import projected_fiscal_documents
from apps.briefing.source_rows import SourceContext, SourceRow
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.fiscal_preview import preview_context
from apps.cycles.fiscal_queries import (
    projected_pending_documents,
    projected_period_document_rows,
)
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.fiscal_snapshot import load_snapshot
from apps.cycles.records import ServiceCycle
from tests.cycles.fiscal_read_assertions import FiscalReads
from tests.cycles.fiscal_review_fixtures import other_cycle
from tests.cycles.test_fiscal_document_review import world
from tests.cycles.test_fiscal_snapshot import _retain_cce
from tests.integrations.capture_fixtures import MONTH, START, review_api, xml
from tests.obligations.fiscal_fixtures import nfse
from tests.obligations.test_fiscal_documents import ingest

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_django.fixtures import SettingsWrapper

    from apps.obligations.models import FiscalDocument
    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)


def record_in(
    world: CaptureWorld,
    fiscal: FiscalDocument,
    cycle: ServiceCycle,
    decision: str = "rejeitado",
    head: FiscalDocumentReview | None = None,
) -> FiscalDocumentReview:
    api = review_api()
    append = getattr(api, "_append_review", api.record_fiscal_document_review)
    with tenant_context(world.tenant_id):
        cycle.refresh_from_db()
        result = cast(
            "FiscalDocumentReview",
            append(
                world.setup.graph.actor,
                cycle.pk,
                fiscal.pk,
                revision_id=fiscal.current_revision_id,
                decision=decision,
                input_digest=api.input_digest(fiscal),
                expected_version=cycle.version,
                expected_head_id=head.pk if head else None,
                note="Motivo sintético",
            ),
        )
        cycle.refresh_from_db()
        return result


def change_state(world: CaptureWorld, fiscal: FiscalDocument, state: str) -> None:
    with tenant_context(world.tenant_id):
        type(fiscal).objects.filter(pk=fiscal.pk).update(fiscal_state=state)
        fiscal.refresh_from_db()


def cycle_gap(
    world: CaptureWorld, gap: str
) -> tuple[
    FiscalDocument,
    ServiceCycle,
    ServiceCycle,
    FiscalDocumentReview | None,
    FiscalDocumentReview,
]:
    first, second = world.setup.cycle, other_cycle(world)
    if gap == "nfse":
        fiscal = cast(
            "FiscalDocument",
            ingest(
                world.setup,
                binding=world.link,
                client_role="tomador",
                source_namespace="focus:nfsen_recebidas",
                payload=nfse().replace(b"2026-09-15", b"2026-10-15"),
            ),
        )
        assert (fiscal.competence, fiscal.issue_date) == (MONTH, date(2026, 10, 15))
    else:
        fiscal = world.ingest()
    current, pending = (second, first) if gap == "earlier" else (first, second)
    old = record_in(world, fiscal, pending) if gap == "stale" else None
    if old:
        change_state(world, fiscal, "cancelada")
    head = record_in(world, fiscal, current)
    return fiscal, current, pending, old, head


def projected_fact(world: CaptureWorld, fiscal: FiscalDocument) -> SourceRow | None:
    with tenant_context(world.tenant_id):
        ctx = SourceContext(ClientCompany.objects.filter(pk=fiscal.client_id), START)
        row = projected_fiscal_documents(ctx).filter(pk=fiscal.pk).first()
    return SourceRow(*row) if row else None


def move_reviewed_document(
    world: CaptureWorld,
) -> tuple[
    FiscalDocument,
    ServiceCycle,
    ServiceCycle,
    list[tuple[FiscalDocumentReview, FiscalDocument]],
]:
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
    return (
        moved,
        first_cycle,
        october,
        [(root, fiscal), (correction, fiscal), (second_root, newer)],
    )


def advance_pending_fact(
    world: CaptureWorld,
    fiscal: FiscalDocument,
    second: ServiceCycle,
    change: str,
    settings: SettingsWrapper,
) -> FiscalDocumentReview | None:
    first = world.setup.cycle
    old_b = record_in(world, fiscal, second) if change == "review-then-cce" else None
    if old_b:
        assert projected_fact(world, fiscal) is None
    if change == "cycle":
        record_in(world, fiscal, first)
    else:
        version, revision = fiscal.version, fiscal.current_revision_id
        _retain_cce(world, settings)
        with tenant_context(world.tenant_id):
            fiscal.refresh_from_db()
        assert (fiscal.version, fiscal.current_revision_id) == (version, revision)
        if old_b:
            with tenant_context(world.tenant_id):
                first_head = FiscalDocumentReview.objects.get(
                    document=fiscal, cycle=first
                )
            record_in(world, fiscal, first, head=first_head)
    return old_b


@pytest.mark.parametrize("gap", ["later", "earlier", "stale", "nfse"])
def test_query_projections_agree_on_the_only_pending_cycle(
    world: CaptureWorld, gap: str
) -> None:
    fiscal, current, pending, old, head = cycle_gap(world, gap)
    with tenant_context(world.tenant_id):
        queue = projected_pending_documents(world.setup.graph.actor)
        assert list(queue.values_list("pk", flat=True)) == [fiscal.pk]
        assert queue.values_list("pending_cycle_id", flat=True).get() == pending.pk
        periods = ServiceCycle.objects.annotate(
            rows=projected_period_document_rows(enabled=True)
        )
        for cycle, decision in ((current, head.decision), (pending, None)):
            (row,) = periods.get(pk=cycle.pk).rows
            assert (row["pk"], row["review_decision"]) == (str(fiscal.pk), decision)
        reads = FiscalReads()
        with reads.capture():
            snapshot = load_snapshot(fiscal.pk)
            preview = preview_context(snapshot, None)
        reads.assert_snapshot()
    assert snapshot.review_material == head.input_digest
    assert cast("dict[str, object]", preview["cycle"])["pk"] == str(pending.pk)
    selected_head = preview["head"]
    assert (
        cast("dict[str, object]", selected_head)["pk"] if selected_head else None
    ) == (str(old.pk) if old else None)
    assert not preview["head_current"]
    fact = projected_fact(world, fiscal)
    assert fact is not None
    assert (fact.subject_ref, fact.target_id) == (fiscal.pk, pending.pk)
    after_write = FiscalReads()
    with tenant_context(world.tenant_id), after_write.capture():
        with after_write.writer():
            review = record_in(world, fiscal, pending, "aceito", old)
        current_preview = preview_context(load_snapshot(fiscal.pk), pending.pk)
    after_write.assert_snapshot()
    assert current_preview["head_current"]
    assert cast("dict[str, object]", current_preview["head"])["pk"] == str(review.pk)
    assert projected_fact(world, fiscal) is None
    with tenant_context(world.tenant_id):
        assert not queue.exists()
        (updated,) = periods.get(pk=pending.pk).rows
        assert updated["review_decision"] == "aceito"


def replace_stale_head[T](
    world: CaptureWorld,
    fiscal: FiscalDocument,
    second: ServiceCycle,
    first_root: FiscalDocumentReview,
    read: Callable[[], T | None],
) -> tuple[T, T, FiscalDocumentReview, FiscalDocumentReview]:
    first = world.setup.cycle
    original = read()
    assert original is not None
    assert (
        record_in(world, fiscal, first, "aceito", first_root).input_digest
        == first_root.input_digest
    )
    assert read() == original
    root = record_in(world, fiscal, second)
    assert root.input_digest == first_root.input_digest
    assert read() is None
    change_state(world, fiscal, "cancelada")
    correction = record_in(world, fiscal, second, "aceito", root)
    change_state(world, fiscal, "autorizada")
    pending = read()
    assert pending is not None
    return original, pending, root, correction


def test_pending_fact_distinguishes_cycle_material_and_terminal_head(
    world: CaptureWorld,
) -> None:
    fiscal, first, second = world.ingest(), world.setup.cycle, other_cycle(world)
    initial = projected_fact(world, fiscal)
    assert initial is not None
    assert initial.target_id == first.pk
    change_state(world, fiscal, "cancelada")
    changed = projected_fact(world, fiscal)
    assert changed is not None
    assert changed.target_id == initial.target_id
    assert changed.fact_version != initial.fact_version
    change_state(world, fiscal, "autorizada")
    root = record_in(world, fiscal, first)
    original, pending, _, head = replace_stale_head(
        world, fiscal, second, root, lambda: projected_fact(world, fiscal)
    )
    assert original.target_id == second.pk
    assert original.fact_version != initial.fact_version
    assert pending.target_id == original.target_id
    assert pending.fact_version != original.fact_version
    with tenant_context(world.tenant_id):
        preview = preview_context(load_snapshot(fiscal.pk), second.pk)
    assert not preview["head_current"]
    assert cast("dict[str, object]", preview["head"])["pk"] == str(head.pk)

"""Exercise the inactive read directly, with no HTTP or template activation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext

from apps.core.tenancy import tenant_context
from apps.cycles.fiscal_snapshot import load_snapshot
from tests.cycles.fiscal_review_fixtures import other_cycle
from tests.cycles.fiscal_review_reader_fixtures import (
    record_in,
    retain_same_revision_cce,
)
from tests.cycles.test_fiscal_document_review import world
from tests.integrations.capture_fixtures import xml

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_django.fixtures import SettingsWrapper

    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_snapshot_retains_both_complete_states_without_lazy_reads(
    world: CaptureWorld, settings: SettingsWrapper
) -> None:
    doc, cycle = world.ingest(), world.setup.cycle
    other = other_cycle(world)
    first = record_in(world, doc, cycle)
    v1 = cycle.version

    def correct() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            retain_same_revision_cce(world, settings)
            record_in(world, doc, cycle, "aceito", first)
        finally:
            connections.close_all()

    advanced = False

    def after_select(
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        nonlocal advanced
        result = execute(sql, params, many, context)
        if not advanced:
            advanced = True
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(correct).result(timeout=30)
        return result

    with (
        tenant_context(world.tenant_id),
        CaptureQueriesContext(connection) as queries,
        connection.execute_wrapper(after_select),
    ):
        before = load_snapshot(doc.pk)
    assert len(queries) == 1
    with tenant_context(world.tenant_id):
        second = type(first).objects.get(supersedes=first)
    assert first.revision_id == second.revision_id
    assert first.input_digest != second.input_digest
    with tenant_context(world.tenant_id), CaptureQueriesContext(connection) as queries:
        after = load_snapshot(doc.pk)
    assert len(queries) == 1
    # Reading both snapshots *after* the correction must not refresh any value.
    with tenant_context(world.tenant_id), CaptureQueriesContext(connection) as queries:
        for snap, head, version, chain, roles in (
            (before, first, v1, {str(first.pk)}, {"xml_original"}),
            (
                after,
                second,
                cycle.version,
                {str(first.pk), str(second.pk)},
                {"xml_original", "xml_cce"},
            ),
        ):
            assert snap.current_revision is not None
            assert snap.current_revision.pk == head.revision_id
            assert snap.review_material == head.input_digest
            cycles = {row["pk"]: row for row in snap.review_cycles}
            assert set(cycles) == {str(cycle.pk), str(other.pk)}
            assert cycles[str(cycle.pk)]["version"] == version
            assert cycles[str(cycle.pk)]["eligible"]
            history = {row["pk"]: row for row in snap.review_history}
            assert set(history) == chain
            active = history[str(head.pk)]
            assert active["revision_id"] == str(head.revision_id)
            assert active["cycle_id"] == str(cycle.pk)
            assert active["input_digest"] == head.input_digest
            assert active["supersedes_id"] == (
                str(first.pk) if head == second else None
            )
            (revision,) = snap.revision_rows
            assert revision["pk"] == str(head.revision_id)
            assert {
                a["role"] for a in revision["artifacts"] if not a["purged_at"]
            } == roles
    assert len(queries) == 0


def test_snapshot_excludes_unrelated_document_history_and_bytes(
    world: CaptureWorld,
) -> None:
    first = world.ingest()
    before = record_in(world, first, world.setup.cycle)
    later = world.ingest(version=2)
    head = record_in(world, later, world.setup.cycle)
    with patch("tests.integrations.capture_fixtures.xml", return_value=xml(2)):
        unrelated = world.ingest(version=3)
    assert unrelated.pk != later.pk
    record_in(world, unrelated, world.setup.cycle)
    with tenant_context(world.tenant_id):
        snap = load_snapshot(later.pk)
    assert snap.current_revision_id == head.revision_id != before.revision_id
    assert snap.review_material == head.input_digest
    assert {row["pk"] for row in snap.revision_rows} == {
        str(before.revision_id),
        str(head.revision_id),
    }
    assert {(r["pk"], r["revision_id"]) for r in snap.review_history} == {
        (str(before.pk), str(before.revision_id)),
        (str(head.pk), str(head.revision_id)),
    }

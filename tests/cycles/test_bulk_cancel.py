"""Cancellation linearizes before the next item claim, preserving prior effects."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItemState, BulkOperationState
from apps.cycles.errors import VersionConflict
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.bulk import cancel_bulk, resume_bulk
from apps.cycles.tasks import run_bulk_operation
from tests.cycles.bulk_fixtures import bulk_setup, confirm, item_states, runtime_call

pytestmark = pytest.mark.django_db(transaction=True)


def test_cancel_before_start_prevents_every_effect_and_automatic_resume() -> None:
    setup = bulk_setup("bulk-cancel-queued")
    operation = confirm(setup.graph.actor, setup.preview())
    with tenant_context(operation.tenant_id):
        cancel_bulk(setup.graph.actor, operation.pk, expected_version=operation.version)
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        operation.refresh_from_db()
        assert operation.state == BulkOperationState.CANCELLED
        assert not ServiceCycle.objects.exists()
        assert not CycleEvent.objects.exists()
        assert set(item_states(operation).values()) == {BulkItemState.CANCELLED}
        with pytest.raises(VersionConflict):
            resume_bulk(
                setup.graph.actor, operation.pk, expected_version=operation.version
            )


def test_cancel_after_committed_item_stops_before_the_next_claim() -> None:
    setup = bulk_setup("bulk-cancel-running")
    operation = confirm(setup.graph.actor, setup.preview())
    committed, release = Event(), Event()

    def pause_after_commit(completed: int) -> None:
        if completed == 1:
            committed.set()
            assert release.wait(15), "The cancellation gate was not released."

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(
            runtime_call,
            lambda: run_bulk_operation(
                str(operation.tenant_id),
                str(operation.pk),
                after_item=pause_after_commit,
            ),
        )
        try:
            assert committed.wait(15), "No item committed before cancellation."
            with tenant_context(operation.tenant_id):
                operation.refresh_from_db()
                cancel_bulk(
                    setup.graph.actor,
                    operation.pk,
                    expected_version=operation.version,
                )
        finally:
            release.set()
        assert worker.result(timeout=20) == 1
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        operation.refresh_from_db()
        assert operation.state == BulkOperationState.CANCELLED
        assert ServiceCycle.objects.count() == 1
        assert CycleEvent.objects.filter(type="cycle.opened").count() == 1
        assert operation.items.filter(state=BulkItemState.DONE).count() == 1
        assert operation.items.filter(state=BulkItemState.CANCELLED).count() == 2
        assert list(
            operation.items.order_by("position").values_list("fence", flat=True)
        ) == [1, 0, 0]


def test_cancel_after_claim_prevents_that_unstarted_effect() -> None:
    setup = bulk_setup("bulk-cancel-claim", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    claimed, release = Event(), Event()

    def hold_claim(_claim: object) -> None:
        claimed.set()
        assert release.wait(15), "The claimed-item gate was not released."

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(
            runtime_call,
            lambda: run_bulk_operation(
                str(operation.tenant_id), str(operation.pk), after_claim=hold_claim
            ),
        )
        try:
            assert claimed.wait(15), "The item was not claimed."
            with tenant_context(operation.tenant_id):
                operation.refresh_from_db()
                cancel_bulk(
                    setup.graph.actor,
                    operation.pk,
                    expected_version=operation.version,
                )
        finally:
            release.set()
        worker.result(timeout=20)
    with tenant_context(operation.tenant_id):
        assert not ServiceCycle.objects.exists()
        assert not CycleEvent.objects.exists()
        operation.refresh_from_db()
        assert operation.state == BulkOperationState.CANCELLED


def test_stale_cancellation_cannot_change_the_current_operation() -> None:
    setup = bulk_setup("bulk-cancel-stale", count=1)
    preview = setup.preview()
    operation = confirm(setup.graph.actor, preview)
    with tenant_context(operation.tenant_id), pytest.raises(VersionConflict):
        cancel_bulk(setup.graph.actor, operation.pk, expected_version=preview.version)
    with tenant_context(operation.tenant_id):
        operation.refresh_from_db()
        assert operation.state == BulkOperationState.QUEUED
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    assert set(item_states(operation).values()) == {BulkItemState.DONE}

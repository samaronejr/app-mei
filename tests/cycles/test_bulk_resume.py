"""S15 M: committed effects survive interruption; stale records do not execute."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.db import connection
from django.db.models import F
from django.utils import timezone

from apps.clients.models import Engagement
from apps.core.tenancy import tenant_context
from apps.cycles.bulk import BulkItemState, BulkOperationState
from apps.cycles.bulk_runner import StaleBulkClaimError, claim_next, execute_claim
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.tasks import run_bulk_operation
from tests.cycles.bulk_fixtures import (
    bulk_setup,
    confirm,
    item_states,
    runtime_call,
)

pytestmark = pytest.mark.django_db(transaction=True)


class WorkerKilledError(RuntimeError):
    """The injected process-loss boundary, deliberately outside item commits."""


def test_killed_worker_resumes_snapshot_skipping_an_edited_record() -> None:
    setup = bulk_setup()
    preview = setup.preview()
    operation = confirm(setup.graph.actor, preview)
    tenant_id = setup.graph.tenant.pk

    def kill_after_first(completed: int) -> None:
        assert not connection.in_atomic_block
        if completed == 1:
            raise WorkerKilledError

    with pytest.raises(WorkerKilledError):
        run_bulk_operation(
            str(tenant_id), str(operation.pk), after_item=kill_after_first
        )
    with tenant_context(tenant_id):
        assert ServiceCycle.objects.count() == 1
        assert CycleEvent.objects.filter(type="cycle.opened").count() == 1
        # A real optimistic edit; the earlier start still covers this competence.
        # Omitting the per-item version guard would wrongly open this cycle.
        Engagement.objects.filter(pk=setup.engagements[1].pk).update(
            starts_on=setup.engagements[1].starts_on - timedelta(days=1),
            version=F("version") + 1,
        )

    run_bulk_operation(str(tenant_id), str(operation.pk))
    run_bulk_operation(str(tenant_id), str(operation.pk))

    assert item_states(operation) == {
        setup.engagements[0].pk: BulkItemState.DONE,
        setup.engagements[1].pk: BulkItemState.SKIPPED_CHANGED,
        setup.engagements[2].pk: BulkItemState.DONE,
    }
    with tenant_context(tenant_id):
        assert set(ServiceCycle.objects.values_list("engagement_id", flat=True)) == {
            setup.engagements[0].pk,
            setup.engagements[2].pk,
        }
        assert CycleEvent.objects.filter(type="cycle.opened").count() == 2
        operation.refresh_from_db()
        assert operation.state == BulkOperationState.COMPLETED
        assert operation.items.filter(state=BulkItemState.DONE).count() == 2


def test_two_interruptions_preserve_the_exact_original_effects() -> None:
    setup = bulk_setup("bulk-twice")
    operation = confirm(setup.graph.actor, setup.preview())

    def stop_after_one(completed: int) -> None:
        if completed == 1:
            raise WorkerKilledError

    for _ in range(2):
        with pytest.raises(WorkerKilledError):
            run_bulk_operation(
                str(operation.tenant_id), str(operation.pk), after_item=stop_after_one
            )
    with tenant_context(operation.tenant_id):
        original = tuple(
            ServiceCycle.objects.order_by("pk").values_list("pk", flat=True)
        )
        assert len(original) == 2
    run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    with tenant_context(operation.tenant_id):
        assert set(original).issubset(ServiceCycle.objects.values_list("pk", flat=True))
        assert ServiceCycle.objects.count() == 3
        assert CycleEvent.objects.filter(type="cycle.opened").count() == 3
        assert operation.items.filter(state=BulkItemState.DONE).count() == 3


def test_expired_claim_is_fenced_before_and_after_the_new_worker_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = bulk_setup("bulk-fenced", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    initial = timezone.now()
    monkeypatch.setattr("apps.cycles.bulk_runner.timezone.now", lambda: initial)
    stale = claim_next(operation.tenant_id, operation.pk)
    assert stale is not None
    # No sleep and no assumption about scheduler timing.
    later = initial + timedelta(minutes=10)
    monkeypatch.setattr("apps.cycles.bulk_runner.timezone.now", lambda: later)
    current = claim_next(operation.tenant_id, operation.pk)
    assert current is not None
    assert current.item_id == stale.item_id
    assert current.fence > stale.fence
    with pytest.raises(StaleBulkClaimError):
        execute_claim(stale)
    execute_claim(current)
    with pytest.raises(StaleBulkClaimError):
        execute_claim(stale)
    with tenant_context(operation.tenant_id):
        assert ServiceCycle.objects.count() == 1
        assert CycleEvent.objects.filter(type="cycle.opened").count() == 1
        assert operation.items.get().state == BulkItemState.DONE


def test_live_claim_cannot_be_stolen_by_a_concurrent_runner() -> None:
    setup = bulk_setup("bulk-live-fence", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    claimed, release = Event(), Event()

    def hold_claim(_claim: object) -> None:
        claimed.set()
        assert release.wait(15), "The live-claim gate was not released."

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            runtime_call,
            lambda: run_bulk_operation(
                str(operation.tenant_id), str(operation.pk), after_claim=hold_claim
            ),
        )
        try:
            assert claimed.wait(15), "The first worker did not commit its claim."
            contender = pool.submit(
                runtime_call,
                lambda: run_bulk_operation(str(operation.tenant_id), str(operation.pk)),
            )
            assert contender.result(timeout=20) == 0
            with tenant_context(operation.tenant_id):
                assert not ServiceCycle.objects.exists()
        finally:
            release.set()
        assert owner.result(timeout=20) == 1
    with tenant_context(operation.tenant_id):
        assert ServiceCycle.objects.count() == 1
        assert CycleEvent.objects.filter(type="cycle.opened").count() == 1


def test_task_refuses_an_enclosing_transaction_instead_of_losing_item_commits() -> None:
    setup = bulk_setup("bulk-outer-transaction", count=1)
    operation = confirm(setup.graph.actor, setup.preview())
    with tenant_context(operation.tenant_id), pytest.raises(RuntimeError):
        run_bulk_operation(str(operation.tenant_id), str(operation.pk))
    assert set(item_states(operation).values()) == {BulkItemState.PENDING}

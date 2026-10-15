"""Concurrent generation yields one cycle and one creator.

The client-row lock serializes these callers. The unique-key fallback for a stale
miss is proven separately in test_cycle_replay_fast_path.py.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import CycleRef, open_cycle
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def test_cycle_generation_race() -> None:
    setup = cycle_setup()
    barrier = Barrier(2, timeout=20)

    def generate() -> CycleRef:
        barrier.wait()
        return open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key="race",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_in_thread_as_runtime, setup.tenant.pk, generate)
            for _ in range(2)
        ]
        refs = [future.result(timeout=30) for future in futures]
    with tenant_context(setup.tenant.pk):
        assert ServiceCycle.objects.count() == 1
        assert CycleEvent.objects.filter(type="cycle.opened").count() == 1
    assert refs[0].id == refs[1].id
    assert sum(ref.created for ref in refs) == 1

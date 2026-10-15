"""Budget admission serializes across tenants, including interrupted workers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.automation.admission import claim
from apps.automation.models import AgentRun
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant
from tests.ai.fixtures import single_company
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


def test_two_firms_contending_for_last_global_reservation(
    setup: PortalSetup,
    settings: SettingsWrapper,
) -> None:
    settings.AI_SHADOW_TENANT_DAILY_BRL = "100"
    other = portal_setup("t24-other-firm")
    Tenant.objects.filter(pk=other.graph.tenant.pk).update(
        features=["cycles_r1", "ai_shadow"]
    )
    first, second = single_company(setup), single_company(other)
    now = timezone.now()
    with tenant_context(setup.graph.tenant.pk):
        AgentRun.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            input_hash="e" * 64,
            model_id="fake-v1",
            reserved_brl=Decimal("49.975"),
            state="provider_error",
            created_at=now,
            expires_at=now + timedelta(days=365),
        )
    ready = Barrier(2, timeout=15)

    def reserve(tid: UUID, iid: UUID) -> bool:
        def work() -> bool:
            ready.wait()
            return claim(tid, iid, now=now) is not None

        return run_in_thread_as_runtime(tid, work)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(reserve, graph.graph.tenant.pk, intake.pk)
            for graph, intake in ((setup, first), (other, second))
        ]
        assert sorted(future.result(timeout=30) for future in futures) == [False, True]
    total = Decimal(0)
    for graph in (setup, other):
        with tenant_context(graph.graph.tenant.pk):
            total += sum(
                AgentRun.objects.values_list("reserved_brl", flat=True), Decimal(0)
            )
    assert total == Decimal("50.000")


def test_repeated_interruptions_never_reclaim_an_uncertain_call(
    setup: PortalSetup,
) -> None:
    intake = single_company(setup)
    tid = setup.graph.tenant.pk
    assert claim(tid, intake.pk, now=timezone.now()) is not None
    # Simulate two redeliveries after death before finalize: the reservation is
    # durable; there is no lease expiry that silently refunds possibly spent money.
    assert claim(tid, intake.pk, now=timezone.now()) is None
    assert claim(tid, intake.pk, now=timezone.now()) is None
    with tenant_context(tid):
        run = AgentRun.objects.get()
        assert run.state == "pending"
        assert run.reserved_brl == Decimal("0.025")

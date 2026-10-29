"""AXRM-015 review 3: the runner that pays for a page settles the consultation.

Every runner here is a real `run_payment_query` (or its Celery task) on its own
`app_runtime` READ COMMITTED connection against the loopback `FakeSerpro`. Hold
points wrap one real service step per runner thread and wait on Events, never on
time; nothing in the protocol, budget, parser, RLS or storage is replaced.
"""

import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.functions import Now

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState
from apps.integrations.models import IntegrationIssue
from apps.integrations.serpro import payments as payment_service
from apps.integrations.serpro import tasks
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.pagtoweb import PageAdapter
from apps.integrations.serpro.payments import (
    PaymentQueryConflictError,
    queue_payment_consultation,
    run_payment_query,
)
from apps.tenants.models import Membership, TenantRole
from tests.integrations.pagtoweb_support import WINDOW, World, darfs, item, page, world
from tests.integrations.test_migration_rollback import _as_migrator
from tests.integrations.test_pagtoweb import (
    _no_budget,
    runner,
    scheduled_query,
    settled,
)
from tests.integrations.test_pagtoweb_schedule import Fire, fire

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime

__all__ = ["fire", "world"]

pytestmark = pytest.mark.django_db(transaction=True)

FAILED = "consulta_pagamentos_falhou"


class WorkerDiedError(BaseException):
    """The worker process vanished here: nothing after this point runs."""


@dataclass
class Stop:
    """Park (or kill) one runner thread at its `at`-th call of a service step."""

    at: int = 1
    dies: bool = False
    drops_session: bool = False
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: int = 0
    records: int | None = None


def stop_at(monkeypatch: pytest.MonkeyPatch, step: str, **stops: Stop) -> None:
    """Gate the real `payments.<step>` per runner thread (named `<stop>_<n>`)."""
    real = getattr(payment_service, step)

    def gated(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - any step
        stop = stops.get(threading.current_thread().name.split("_")[0])
        if stop is not None:
            stop.calls += 1
            if stop.calls == stop.at:
                for arg in args:
                    if isinstance(arg, PageAdapter) and arg.records is not None:
                        stop.records = len(arg.records)
                if stop.drops_session:
                    session_ends()
                stop.entered.set()
                if stop.dies:
                    raise WorkerDiedError
                assert stop.release.wait(timeout=20)
        return real(*args, **kwargs)

    monkeypatch.setattr(payment_service, step, gated)


def session_ends() -> None:
    """This runner's database session ends while its thread lives on.

    Its session lock goes with the connection, so the runner is presumed dead; the
    thread reconnects under the same runtime role, as Django does after a loss.
    """
    connection.close()
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE app_runtime")


def untouched(world: World, query_id: UUID) -> dict[str, object]:
    """Everything a yielding runner must leave alone, the whole row included."""
    view = settled(world, query_id)
    with tenant_context(world.termo.tenant.pk):
        view["row"] = PaymentQuery.objects.filter(pk=query_id).values().get()
        view["events"] = Event.objects.count()
    return view


def stored(world: World, query_id: UUID) -> PaymentQuery:
    with tenant_context(world.termo.tenant.pk):
        return PaymentQuery.objects.get(pk=query_id)


def lease_runs_out(world: World, query_id: UUID) -> None:
    """Time passes beyond the owner's lease: its deadline moves into the past.

    Only a transaction declared as the live owner may write the row, so the
    deadline is moved under the owner's own token.
    """
    with tenant_context(world.termo.tenant.pk):
        owner = (
            PaymentQuery.objects.filter(pk=query_id)
            .values_list("settlement_owner", flat=True)
            .get()
        )
        assert owner is not None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                [payment_service.OWNER_SETTING, str(owner)],
            )
        PaymentQuery.objects.filter(pk=query_id).update(
            settlement_until=Now() - timedelta(seconds=1)
        )


def run(query: PaymentQuery) -> str:
    return run_payment_query(query.tenant_id, query.pk).state


def redeliver(query: PaymentQuery) -> str:
    args = (str(query.tenant_id), str(query.pk))
    state: str = tasks.run_payment_query_task.apply(args=args).get()
    return state


def test_a_runner_arriving_after_the_owner_paid_leaves_the_page_to_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given A's paid page call is finalized and A holds its parsed records.
    query = scheduled_query(world)
    owner = Stop()
    stop_at(monkeypatch, "_settle_page", owner=owner)
    world.script(page([item("DAS-CONCURRENT-01")]))
    pool, first = runner("owner", lambda: run(query))
    try:
        assert owner.entered.wait(timeout=10)
        assert owner.records == 1
        held = stored(world, query.pk)
        assert (held.state, held.pages_fetched) == ("em_andamento", 0)
        assert [(call.state, call.billed) for call in world.calls()] == [
            (CallState.SUCCESS, "sim")
        ]
        before = untouched(world, query.pk)
        # When B runs the same consultation on another connection.
        late_pool, late = runner("late", lambda: run(query))
        assert late.result(timeout=25) == "em_andamento"
        late_pool.shutdown(wait=True)
        # Then B wrote nothing: no state, receipt, event or issue.
        assert untouched(world, query.pk) == before
    finally:
        owner.release.set()
        pool.shutdown(wait=True)
    # And A, released, records its page: one paid request, one DAS.
    assert first.result(timeout=25) == "encontrado"
    view = settled(world, query.pk)
    assert view["query"][:4] == ("encontrado", "", 1, 1)  # type: ignore[index]
    assert view["receipts"] == [("encontrado", 1)]
    assert (view["payments"], view["requests"], view["finished_events"]) == (1, 1, 1)


def test_three_runners_leave_the_paid_page_to_the_one_that_paid(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given B and C read the active consultation and wait at their reservation.
    query = scheduled_query(world)
    second, third, owner = Stop(), Stop(), Stop()
    stop_at(monkeypatch, "reserve", second=second, third=third)
    stop_at(monkeypatch, "_settle_page", owner=owner)
    world.script(page([item("DAS-CONCURRENT-02")]))
    pools: list[ThreadPoolExecutor] = []
    try:
        pool, late_b = runner("second", lambda: run(query))
        pools.append(pool)
        pool, late_c = runner("third", lambda: run(query))
        pools.append(pool)
        assert second.entered.wait(timeout=10)
        assert third.entered.wait(timeout=10)
        # And A then pays for page 1 and holds its records before settling.
        pool, first = runner("owner", lambda: run(query))
        pools.append(pool)
        assert owner.entered.wait(timeout=10)
        assert owner.records == 1
        before = untouched(world, query.pk)
        # When B and C resume against the same completed call.
        second.release.set()
        third.release.set()
        assert late_b.result(timeout=25) == "em_andamento"
        assert late_c.result(timeout=25) == "em_andamento"
        # Then neither settles it nor writes anything.
        assert untouched(world, query.pk) == before
    finally:
        for stop in (second, third, owner):
            stop.release.set()
        for pool in pools:
            pool.shutdown(wait=True)
    assert first.result(timeout=25) == "encontrado"
    view = settled(world, query.pk)
    assert view["query"][:4] == ("encontrado", "", 1, 1)  # type: ignore[index]
    assert view["receipts"] == [("encontrado", 1)]
    assert view["requests"] == 1


def test_the_owner_held_mid_paging_keeps_the_consultation(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given A applied page 1 and waits before reserving page 2.
    query = scheduled_query(world)
    between, paid = Stop(at=2), Stop(at=2)
    stop_at(monkeypatch, "reserve", owner=between)
    stop_at(monkeypatch, "_settle_page", owner=paid)
    world.script(page(darfs(100)), page(darfs(1, start=100)))
    pool, first = runner("owner", lambda: run(query))
    try:
        assert between.entered.wait(timeout=10)
        assert stored(world, query.pk).pages_fetched == 1
        before = untouched(world, query.pk)
        # When the task is redelivered between A's pages.
        late_pool, late = runner("late", lambda: redeliver(query))
        assert late.result(timeout=25) == "em_andamento"
        late_pool.shutdown(wait=True)
        # Then the redelivery neither fetches page 2 nor writes anything.
        assert untouched(world, query.pk) == before
        # And when A paid for page 2 and holds it, another delivery yields too.
        between.release.set()
        assert paid.entered.wait(timeout=10)
        assert paid.records == 1
        before = untouched(world, query.pk)
        later_pool, later = runner("later", lambda: redeliver(query))
        assert later.result(timeout=25) == "em_andamento"
        later_pool.shutdown(wait=True)
        assert untouched(world, query.pk) == before
    finally:
        between.release.set()
        paid.release.set()
        pool.shutdown(wait=True)
    assert first.result(timeout=25) == "encontrado"
    view = settled(world, query.pk)
    assert view["query"][:4] == ("encontrado", "", 2, 101)  # type: ignore[index]
    assert view["receipts"] == [("encontrado", 100), ("encontrado", 1)]
    assert view["requests"] == 2
    assert world.offsets() == [0, 100]


def test_a_staff_click_racing_the_scheduled_consultation_takes_nothing_from_it(
    world: World, monkeypatch: pytest.MonkeyPatch, fire: Fire
) -> None:
    # Given beat's scheduled consultation paid for its page and holds it.
    tenant_id = world.termo.tenant.pk
    paid = Stop()
    stop_at(monkeypatch, "_settle_page", sched=paid)
    world.script(page([item("DAS-SCHEDULED-01")]))
    pool, beat = runner("sched", lambda: str(fire(date(2026, 10, 27))))
    try:
        assert paid.entered.wait(timeout=10)
        with tenant_context(tenant_id):
            scheduled = PaymentQuery.objects.get()
        before = untouched(world, scheduled.pk)
        # When staff click for a consultation of their own, beat fires the next
        # day and the scheduled task is redelivered.
        with (
            tenant_context(tenant_id),
            pytest.raises(PaymentQueryConflictError),
        ):
            queue_payment_consultation(
                world.termo.actor,
                world.termo.client.pk,
                WINDOW,
                expected_latest=scheduled.pk,
                nonce="staff-race",
            )
        assert fire(date(2026, 10, 28)) == 0
        assert redeliver(scheduled) == "em_andamento"
        # Then none of them wrote anything or reached the Receita.
        assert untouched(world, scheduled.pk) == before
        with tenant_context(tenant_id):
            assert PaymentQuery.objects.count() == 1
    finally:
        paid.release.set()
        pool.shutdown(wait=True)
    assert beat.result(timeout=25) == "1"
    assert stored(world, scheduled.pk).state == "encontrado"
    # And once the scheduled one is settled, the staff click is one paid request.
    world.script(page([item("DAS-STAFF-01")]))
    with tenant_context(tenant_id):
        staff = queue_payment_consultation(
            world.termo.actor,
            world.termo.client.pk,
            WINDOW,
            expected_latest=scheduled.pk,
            nonce="staff-after",
        )
    assert stored(world, staff.pk).state == "encontrado"
    assert stored(world, scheduled.pk).state == "encontrado"
    assert len(world.sent()) == 2


def test_an_initiator_losing_access_never_downgrades_the_owners_paid_page(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,  # noqa: ANN401 - pytest-django fixture
) -> None:
    # Given a staff consultation whose runner paid for its page and holds it.
    actor = world.termo.actor
    with tenant_context(world.termo.tenant.pk), django_capture_on_commit_callbacks():
        query = queue_payment_consultation(
            actor, world.termo.client.pk, WINDOW, expected_latest=None, nonce="s"
        )
    owner = Stop()
    stop_at(monkeypatch, "_settle_page", owner=owner)
    world.script(page([item("DAS-STAFF-02")]))
    pool, first = runner("owner", lambda: run(query))
    try:
        assert owner.entered.wait(timeout=10)
        # When the initiator loses access and the task is redelivered.
        with tenant_context(world.termo.tenant.pk):
            Membership.objects.filter(user=actor).update(
                role=TenantRole.STAFF_ACCOUNTANT
            )
        before = untouched(world, query.pk)
        late_pool, late = runner("late", lambda: redeliver(query))
        # Then the redelivery yields instead of failing the owner's consultation.
        assert late.result(timeout=25) == "em_andamento"
        late_pool.shutdown(wait=True)
        assert untouched(world, query.pk) == before
    finally:
        owner.release.set()
        pool.shutdown(wait=True)
    # And the owner records the page it paid for.
    assert first.result(timeout=25) == "encontrado"
    assert settled(world, query.pk)["receipts"] == [("encontrado", 1)]


@pytest.mark.parametrize("dies_at", ["after-paid-page", "between-pages"])
def test_a_dead_owners_consultation_resumes_on_redelivery_once_its_lease_ends(
    world: World, monkeypatch: pytest.MonkeyPatch, dies_at: str
) -> None:
    # Given A dies after its page call was paid, or after applying page 1.
    query = scheduled_query(world)
    if dies_at == "after-paid-page":
        dead = Stop(dies=True)
        stop_at(monkeypatch, "_settle_page", owner=dead)
        world.script(page([item("DAS-ORPHAN-01")]))
    else:
        dead = Stop(at=2, dies=True)
        stop_at(monkeypatch, "reserve", owner=dead)
        world.script(page(darfs(100)), page(darfs(1, start=100)))
    pool, first = runner("owner", lambda: run(query))
    assert dead.entered.wait(timeout=10)
    with pytest.raises(WorkerDiedError):
        first.result(timeout=25)
    pool.shutdown(wait=True)
    before = untouched(world, query.pk)
    # When the task is redelivered while A's lease is still live, it yields.
    assert redeliver(query) == "em_andamento"
    assert untouched(world, query.pk) == before
    # And once the lease has run out, the next delivery takes over and resumes.
    lease_runs_out(world, query.pk)
    resumed = redeliver(query)
    view = settled(world, query.pk)
    if dies_at == "after-paid-page":
        # The paid page's records died with A; it is never fetched again.
        assert resumed == "incerto"
        lost = ("incerto", "pagina_nao_registrada", 0)
        assert view["query"][:3] == lost  # type: ignore[index]
        assert (view["payments"], view["requests"]) == (0, 1)
        with tenant_context(world.termo.tenant.pk):
            assert IntegrationIssue.objects.filter(reason=FAILED).count() == 1
    else:
        assert resumed == "encontrado"
        assert view["query"][:4] == ("encontrado", "", 2, 101)  # type: ignore[index]
        assert view["receipts"] == [("encontrado", 100), ("encontrado", 1)]
        assert world.offsets() == [0, 100]


def test_an_owner_whose_lease_ran_out_yields_to_the_runner_that_took_over(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given A holds a paid page past its lease and C took the consultation over.
    query = scheduled_query(world)
    owner, successor = Stop(), Stop()
    # A's database session is gone too: only then is it presumed dead.
    owner.drops_session = True
    stop_at(monkeypatch, "_settle_page", owner=owner, next=successor)
    world.script(page([item("DAS-LATE-01")]))
    pools: list[ThreadPoolExecutor] = []
    try:
        pool, first = runner("owner", lambda: run(query))
        pools.append(pool)
        assert owner.entered.wait(timeout=10)
        lease_runs_out(world, query.pk)
        pool, taken = runner("next", lambda: redeliver(query))
        pools.append(pool)
        assert successor.entered.wait(timeout=10)
        before = untouched(world, query.pk)
        # When A comes back.
        owner.release.set()
        # Then it yields to the live owner and writes nothing.
        assert first.result(timeout=25) == "em_andamento"
        assert untouched(world, query.pk) == before
    finally:
        owner.release.set()
        successor.release.set()
        for pool in pools:
            pool.shutdown(wait=True)
    # And the new owner settles the page whose records it never received.
    assert taken.result(timeout=25) == "incerto"
    view = settled(world, query.pk)
    lost = ("incerto", "pagina_nao_registrada", 0)
    assert view["query"][:3] == lost  # type: ignore[index]
    assert view["requests"] == 1


def test_a_stale_runner_never_resettles_an_applied_page_once_the_lease_ends(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given B waits at page 1's reservation, A applied page 1 and waits before
    # page 2, and A's lease has run out.
    query = scheduled_query(world)
    stale, between = Stop(), Stop(at=2)
    stop_at(monkeypatch, "reserve", late=stale, owner=between)
    world.script(page(darfs(100)), page(darfs(1, start=100)))
    pools: list[ThreadPoolExecutor] = []
    try:
        pool, late = runner("late", lambda: run(query))
        pools.append(pool)
        assert stale.entered.wait(timeout=10)
        pool, first = runner("owner", lambda: run(query))
        pools.append(pool)
        assert between.entered.wait(timeout=10)
        lease_runs_out(world, query.pk)
        before = untouched(world, query.pk)
        # When B resumes behind the applied page.
        stale.release.set()
        # Then it neither takes the consultation nor settles page 1 again.
        assert late.result(timeout=25) == "em_andamento"
        assert untouched(world, query.pk) == before
    finally:
        stale.release.set()
        between.release.set()
        for pool in pools:
            pool.shutdown(wait=True)
    # And A, nobody having taken over, renews its lease and completes alone.
    assert first.result(timeout=25) == "encontrado"
    view = settled(world, query.pk)
    assert view["query"][:4] == ("encontrado", "", 2, 101)  # type: ignore[index]
    assert view["requests"] == 2


def test_a_refused_runner_never_rewrites_a_consultation_finished_without_it(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,  # noqa: ANN401 - pytest-django fixture
) -> None:
    # Given B passed its checks and waits at its reservation.
    actor = world.termo.actor
    tenant_id = world.termo.tenant.pk
    with tenant_context(tenant_id), django_capture_on_commit_callbacks():
        query = queue_payment_consultation(
            actor, world.termo.client.pk, WINDOW, expected_latest=None, nonce="r"
        )
    late = Stop()
    stop_at(monkeypatch, "reserve", late=late)
    pool, stale = runner("late", lambda: run(query))
    try:
        assert late.entered.wait(timeout=10)
        # When the initiator loses access, A finishes the consultation and the
        # budget is gone before B's reservation is admitted.
        with tenant_context(tenant_id):
            Membership.objects.filter(user=actor).update(
                role=TenantRole.STAFF_ACCOUNTANT
            )
        assert run(query) == "falha"
        _no_budget(world)
        before = settled(world, query.pk)
    finally:
        late.release.set()
        pool.shutdown(wait=True)
    # Then B's refusal leaves A's outcome, receipts and work as they were.
    assert stale.result(timeout=25) == "falha"
    after = settled(world, query.pk)
    for key in ("query", "receipts", "finished_events", "payments", "requests"):
        assert after[key] == before[key]
    with tenant_context(tenant_id):
        assert IntegrationIssue.objects.filter(reason=FAILED).count() == 1
        assert stored(world, query.pk).failure_code == "solicitante_sem_permissao"


def test_the_database_refuses_any_writer_but_the_live_owner(world: World) -> None:
    query = scheduled_query(world)
    tenant_id = world.termo.tenant.pk
    owner, other = uuid4(), uuid4()
    assert payment_service._own(query, 0, owner)

    def write(as_runner: UUID | None) -> None:
        with tenant_context(tenant_id), transaction.atomic():
            if as_runner is not None:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config(%s, %s, true)",
                        [payment_service.OWNER_SETTING, str(as_runner)],
                    )
            PaymentQuery.objects.filter(pk=query.pk).update(
                state="incerto", failure_code="sobrescrito"
            )

    # Another runner, declared or not, and the table owner are refused.
    for as_runner in (None, other):
        with pytest.raises(DatabaseError, match="settled by a live runner"):
            write(as_runner)
    with _as_migrator(), pytest.raises(DatabaseError, match="live runner"):
        write(None)
    assert stored(world, query.pk).state == "na_fila"

    # A lease marker is always an owner with its deadline.
    def drop_deadline() -> None:
        with tenant_context(tenant_id), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config(%s, %s, true)",
                    [payment_service.OWNER_SETTING, str(owner)],
                )
            PaymentQuery.objects.filter(pk=query.pk).update(settlement_until=None)

    with pytest.raises(IntegrityError, match="paymentquery_owner_lease"):
        drop_deadline()
    # Once the lease has run out, the owner is presumed dead and others may write.
    lease_runs_out(world, query.pk)
    write(None)
    assert stored(world, query.pk).failure_code == "sobrescrito"


def test_the_owner_guard_reverses_populated_and_comes_back(world: World) -> None:
    # Given a consultation with a live lease on file.
    query = scheduled_query(world)
    assert payment_service._own(query, 0, uuid4())
    with _as_migrator():
        # When the guard migration is reversed with that row present.
        MigrationExecutor(connection).migrate(
            [("integrations", "0007_payment_queries")]
        )
        # Then any writer may update it again.
        with tenant_context(world.termo.tenant.pk), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE integrations_paymentquery SET failure_code = 'x' WHERE id = %s",
                [query.pk],
            )
    # And re-applied (on leaving the migrator), the row survives with no lease.
    reapplied = stored(world, query.pk)
    assert (reapplied.state, reapplied.failure_code) == ("na_fila", "x")
    assert (reapplied.settlement_owner, reapplied.settlement_until) == (None, None)
    assert payment_service._own(reapplied, 0, uuid4())
    with (
        pytest.raises(DatabaseError, match="live runner"),
        tenant_context(world.termo.tenant.pk),
        transaction.atomic(),
    ):
        PaymentQuery.objects.filter(pk=query.pk).update(failure_code="y")


# Review 4 (AXRM015-RV4-*): a stalled owner keeps its page, a yielded delivery
# comes back by itself, and no abandoned page keeps the firm's cap.

PAGE_MICROBRL = 240_000  # One SP-PAG page at the world's 0.24 BRL tariff.


def cap_admits(world: World, pages: int) -> None:
    """From now on the firm's cap admits exactly `pages` more SP-PAG pages."""
    with tenant_context(world.termo.tenant.pk):
        budget = ProviderBudget.objects.get()
        used = budget.known_liability + budget.uncertain_exposure + budget.reserved
        ProviderBudget.objects.filter(pk=budget.pk).update(
            cap_microbrl=used + pages * PAGE_MICROBRL
        )


def access(world: World, role: str | None = None) -> str:
    """Set the initiator's role (None reads it); returns the role it had."""
    actor = world.termo.actor
    with tenant_context(world.termo.tenant.pk):
        had = Membership.objects.filter(user=actor).values_list("role", flat=True).get()
        if role is not None:
            Membership.objects.filter(user=actor).update(role=role)
    return had


def unused_pages_returned(world: World) -> None:
    """Nothing was sent, the page call is cancelled and its money is back."""
    assert world.budget().reserved == 0
    assert [(c.state, c.attempts, c.provider_code) for c in world.calls()] == [
        (CallState.CANCELLED, 0, "consulta_encerrada")
    ]
    with tenant_context(world.termo.tenant.pk):
        held = ProviderReservation.objects.values_list("state", flat=True)
        assert list(held) == ["released"]
    assert world.sent() == []


def staff_retry_is_admitted(
    world: World,
    previous: PaymentQuery,
    capture: Any,  # noqa: ANN401 - pytest-django fixture
) -> None:
    """The deliberate retry pays for the one page the cap admits, and records it."""
    world.script(page([item("DAS-RETRY-01")]))
    with tenant_context(world.termo.tenant.pk), capture():
        retry = queue_payment_consultation(
            world.termo.actor,
            world.termo.client.pk,
            WINDOW,
            expected_latest=previous.pk,
            nonce=f"retry-{previous.pk}",
        )
    assert run(retry) == "encontrado"
    assert len(world.sent()) == 1
    # Its own paid page stays settled: only never-claimed pages are released.
    assert (world.calls()[-1].state, world.calls()[-1].billed) == (
        CallState.SUCCESS,
        "sim",
    )


def test_a_stalled_owner_keeps_its_paid_page_past_its_lease(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given A paid for its page and stalls before settling it, past its lease,
    # while its process and its database session live on.
    query = scheduled_query(world)
    owner = Stop()
    stop_at(monkeypatch, "_settle_page", owner=owner)
    world.script(page([item("DAS-STALLED-01")]))
    pool, first = runner("owner", lambda: run(query))
    try:
        assert owner.entered.wait(timeout=10)
        assert owner.records == 1
        lease_runs_out(world, query.pk)
        before = untouched(world, query.pk)
        # When B runs the consultation on another connection.
        late_pool, late = runner("late", lambda: run(query))
        # Then B yields and writes nothing: A is late, not dead.
        assert late.result(timeout=25) == "em_andamento"
        late_pool.shutdown(wait=True)
        assert untouched(world, query.pk) == before
    finally:
        owner.release.set()
        pool.shutdown(wait=True)
    # And A, released, records the page it paid for.
    assert first.result(timeout=25) == "encontrado"
    view = settled(world, query.pk)
    assert view["query"][:4] == ("encontrado", "", 1, 1)  # type: ignore[index]
    assert view["receipts"] == [("encontrado", 1)]
    assert (view["payments"], view["requests"], view["finished_events"]) == (1, 1, 1)


def test_a_delivery_yielding_to_a_dead_owner_schedules_its_own_recovery(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given A applied page 1 and died before page 2, its lease still live.
    query = scheduled_query(world)
    dead = Stop(at=2, dies=True)
    stop_at(monkeypatch, "reserve", owner=dead)
    world.script(page(darfs(100)), page(darfs(1, start=100)))
    pool, first = runner("owner", lambda: run(query))
    assert dead.entered.wait(timeout=10)
    with pytest.raises(WorkerDiedError):
        first.result(timeout=25)
    pool.shutdown(wait=True)
    scheduled: list[tuple[tuple[str, ...], datetime]] = []

    def later(args: tuple[str, ...], eta: "datetime", *, ignore_result: bool) -> None:
        assert ignore_result
        scheduled.append((tuple(args), eta))

    monkeypatch.setattr(tasks.run_payment_query_task, "apply_async", later)
    # When the only redelivery arrives before the lease ends.
    assert redeliver(query) == "em_andamento"
    # Then it schedules the consultation again for after the lease's deadline.
    deadline = stored(world, query.pk).settlement_until
    assert deadline is not None
    assert len(scheduled) == 1
    args, eta = scheduled[0]
    assert args == (str(query.tenant_id), str(query.pk))
    assert eta > deadline
    # And a continuation still finding the lease live yields and looks again.
    assert tasks.run_payment_query_task.apply(args=args).get() == "em_andamento"
    assert len(scheduled) == 2
    assert scheduled[1][1] > deadline
    # And once the lease has run out, the continuation resumes by itself.
    lease_runs_out(world, query.pk)
    assert tasks.run_payment_query_task.apply(args=scheduled[1][0]).get() == (
        "encontrado"
    )
    assert len(scheduled) == 2
    view = settled(world, query.pk)
    assert view["query"][:4] == ("encontrado", "", 2, 101)  # type: ignore[index]
    assert view["receipts"] == [("encontrado", 100), ("encontrado", 1)]
    assert world.offsets() == [0, 100]


def test_a_stale_runner_reserving_after_the_finish_returns_the_page_to_the_cap(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,  # noqa: ANN401 - pytest-django fixture
) -> None:
    # Given B passed its checks and waits at its reservation.
    with tenant_context(world.termo.tenant.pk), django_capture_on_commit_callbacks():
        query = queue_payment_consultation(
            world.termo.actor,
            world.termo.client.pk,
            WINDOW,
            expected_latest=None,
            nonce="u",
        )
    late = Stop()
    stop_at(monkeypatch, "reserve", late=late)
    pool, stale = runner("late", lambda: run(query))
    try:
        assert late.entered.wait(timeout=10)
        # When the initiator loses access, A finishes the consultation without
        # any page, and the firm's cap admits exactly one more page.
        role = access(world, TenantRole.STAFF_ACCOUNTANT)
        assert run(query) == "falha"
        cap_admits(world, 1)
    finally:
        late.release.set()
        pool.shutdown(wait=True)
    # Then B's reservation for the finished consultation goes back to the cap.
    assert stale.result(timeout=25) == "falha"
    unused_pages_returned(world)
    assert stored(world, query.pk).failure_code == "solicitante_sem_permissao"
    # And once access is back, the deliberate staff retry is admitted.
    access(world, role)
    staff_retry_is_admitted(world, query, django_capture_on_commit_callbacks)


def test_a_page_reserved_by_a_runner_that_died_is_released_when_it_ends(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,  # noqa: ANN401 - pytest-django fixture
) -> None:
    # Given A reserved page 1 and died before taking the settlement lease.
    with tenant_context(world.termo.tenant.pk), django_capture_on_commit_callbacks():
        query = queue_payment_consultation(
            world.termo.actor,
            world.termo.client.pk,
            WINDOW,
            expected_latest=None,
            nonce="d",
        )
    dead = Stop(dies=True)
    stop_at(monkeypatch, "_own", owner=dead)
    pool, first = runner("owner", lambda: run(query))
    assert dead.entered.wait(timeout=10)
    with pytest.raises(WorkerDiedError):
        first.result(timeout=25)
    pool.shutdown(wait=True)
    cap_admits(world, 0)
    # When the initiator loses access and the next delivery ends the consultation.
    role = access(world, TenantRole.STAFF_ACCOUNTANT)
    assert run(query) == "falha"
    # Then the page nobody will send goes back to the cap.
    unused_pages_returned(world)
    # And once access is back, the deliberate staff retry is admitted.
    access(world, role)
    staff_retry_is_admitted(world, query, django_capture_on_commit_callbacks)


def overwrite(world: World, query_id: UUID) -> None:
    with tenant_context(world.termo.tenant.pk), transaction.atomic():
        PaymentQuery.objects.filter(pk=query_id).update(
            state="incerto", failure_code="sobrescrito"
        )


def test_the_database_refuses_another_writer_while_the_owners_session_lives(
    world: World,
) -> None:
    # Given an owner whose lease ran out while its connection still holds its
    # session lock (keyed as `integrations.0009` reads it).
    query = scheduled_query(world)
    owner = uuid4()
    assert payment_service._own(query, 0, owner)
    lease_runs_out(world, query.pk)
    held, done = threading.Event(), threading.Event()

    def owner_session() -> str:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", [str(owner)]
            )
        held.set()
        assert done.wait(timeout=20)
        return "closed"

    pool, session = runner("session", owner_session)
    try:
        assert held.wait(timeout=10)
        # When another writer updates the consultation.
        with pytest.raises(DatabaseError, match="settled by a live runner"):
            overwrite(world, query.pk)
        assert stored(world, query.pk).state == "na_fila"
    finally:
        done.set()
        pool.shutdown(wait=True)
    # Then only once that connection is gone may another writer proceed.
    assert session.result(timeout=25) == "closed"
    overwrite(world, query.pk)
    assert stored(world, query.pk).failure_code == "sobrescrito"


def test_a_stale_runner_behind_a_dead_owners_applied_page_leaves_it_to_resume(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given B waits at page 1's reservation, A applied page 1 and died before
    # page 2, and A's lease has run out: A is really gone this time.
    query = scheduled_query(world)
    stale, dead = Stop(), Stop(at=2, dies=True)
    stop_at(monkeypatch, "reserve", late=stale, owner=dead)
    world.script(page(darfs(100)), page(darfs(1, start=100)))
    pool, late = runner("late", lambda: run(query))
    try:
        assert stale.entered.wait(timeout=10)
        owner_pool, first = runner("owner", lambda: run(query))
        assert dead.entered.wait(timeout=10)
        with pytest.raises(WorkerDiedError):
            first.result(timeout=25)
        owner_pool.shutdown(wait=True)
        lease_runs_out(world, query.pk)
        before = untouched(world, query.pk)
        # When B resumes behind the applied page.
        stale.release.set()
        # Then it takes nothing: page 1 was settled and is never settled again.
        assert late.result(timeout=25) == "em_andamento"
        assert untouched(world, query.pk) == before
    finally:
        stale.release.set()
        pool.shutdown(wait=True)
    # And the next delivery resumes at page 2.
    assert run(query) == "encontrado"
    view = settled(world, query.pk)
    assert view["query"][:4] == ("encontrado", "", 2, 101)  # type: ignore[index]
    assert view["requests"] == 2
    assert world.offsets() == [0, 100]

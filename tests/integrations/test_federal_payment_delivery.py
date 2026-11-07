"""Payment identity and queue ownership regressions from review 6."""

from datetime import date

import pytest

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.serpro import sweeps, tasks
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.pagtoweb import PaymentRange
from tests.integrations.pagtoweb_support import World, page, world
from tests.integrations.test_federal_sweeps import add_clients, lane, on

__all__ = ["lane", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_lost_payment_handoff_recovers_original_monthly_identity_on_later_days(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2.4: lost payment enqueue is requeued the next day.

    PLAN18 requires "monthly checks run without manual effort" and
    "one consultation per month for every client with active `pagtoweb` authority".
    Review 1 additionally requires nothing be "left unowned".
    """

    def broken(tenant_id: str, query_id: str) -> None:
        message = "lost payment handoff"
        raise ConnectionError(message)

    monkeypatch.setattr(tasks.run_payment_query_task, "delay", broken)
    on(monkeypatch, date(2026, 10, 21))
    first = tasks.schedule_payment_consultations.apply()
    assert first.failed()
    with tenant_context(world.termo.tenant.pk):
        original = PaymentQuery.objects.get()
        assert original.state == "na_fila"
    world.script(page([]))

    def run_now(tenant_id: str, query_id: str) -> object:
        return tasks.run_payment_query_task.apply(args=(tenant_id, query_id)).get()

    monkeypatch.setattr(tasks.run_payment_query_task, "delay", run_now)
    counts = []
    for day in [22, 23, 24]:
        on(monkeypatch, date(2026, 10, day))
        counts.append(tasks.schedule_payment_consultations.apply().get())
    with tenant_context(world.termo.tenant.pk):
        queries = list(PaymentQuery.objects.values_list("pk", "state"))
    assert len(world.sent()) == 1
    assert counts == [1, 0, 0]
    assert queries == [(original.pk, "nada_localizado")]


def test_one_turn_drains_each_owned_payment_handoff_once(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_clients(world, 4, mei=True)
    deliveries: list[tuple[str, ...]]
    deliveries = []
    monkeypatch.setattr(
        tasks.run_payment_query_task, "delay", lambda *args: deliveries.append(args)
    )
    on(monkeypatch, date(2026, 10, 21))
    assert tasks.schedule_payment_consultations.apply().get() == 5
    with tenant_context(world.termo.tenant.pk):
        queries = set(PaymentQuery.objects.values_list("pk", flat=True))
    expected = {(str(world.termo.tenant.pk), str(pk)) for pk in queries}
    assert len(deliveries) == 5
    assert set(deliveries) == expected


def test_payment_deliveries_are_once_per_client_and_month_across_repeated_turns(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN18: "one consultation per month for every client with active
    `pagtoweb` authority"; R2.4 retains the original lost monthly intent.
    """
    add_clients(world, 4, mei=True)
    with tenant_context(world.termo.tenant.pk):
        # Ten synthetic consultations cost 2.40; both calendar scenarios execute
        # during the same real billing month.
        ProviderBudget.objects.update(cap_microbrl=4_000_000)
    deliveries: list[tuple[str, str]]
    deliveries = []

    def deliver(tenant_id: str, query_id: str) -> object:
        deliveries.append((tenant_id, query_id))
        return tasks.run_payment_query_task.apply(args=(tenant_id, query_id)).get()

    monkeypatch.setattr(tasks.run_payment_query_task, "delay", deliver)
    month_queries = []
    for month in (10, 11):
        world.script(*(page([]) for _ in range(5)))
        counts = []
        for day in (21, 22, 23):
            on(monkeypatch, date(2026, month, day))
            counts.append(tasks.schedule_payment_consultations.apply().get())
            assert sweeps._pending == []
        assert counts == [5, 0, 0]
        with tenant_context(world.termo.tenant.pk):
            rows = list(
                PaymentQuery.objects.filter(range_start=date(2026, month - 1, 1))
                .order_by("client_id")
                .values_list("pk", "client_id", "state")
            )
        assert len(rows) == 5
        assert len({client for _, client, _ in rows}) == 5
        assert all(state == "nada_localizado" for _, _, state in rows)
        month_queries.append({pk for pk, _, _ in rows})
    assert month_queries[0].isdisjoint(month_queries[1])
    assert len(deliveries) == len(set(deliveries)) == 10
    assert len(world.sent()) == 10


def test_failed_client_does_not_carry_an_uncommitted_pending_handoff(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback clears its pending handoff; later turns recover the same client."""
    clients = add_clients(world, 4, mei=True)
    failed_client = clients[0].pk
    original = sweeps.payments_for
    deliveries: list[tuple[str, str]] = []
    fail = True

    def act(client: ClientCompany, window: PaymentRange) -> sweeps.Outcome:
        outcome = original(client, window)
        if client.pk == failed_client and fail:
            message = "interrupt before client commit"
            raise ConnectionError(message)
        return outcome

    def deliver(tenant_id: str, query_id: str) -> object:
        deliveries.append((tenant_id, query_id))
        result = tasks.run_payment_query_task.apply(args=(tenant_id, query_id))
        assert result.successful(), result.result
        return result.get()

    monkeypatch.setattr(sweeps, "payments_for", act)
    monkeypatch.setattr(tasks.run_payment_query_task, "delay", deliver)
    world.script(*(page([]) for _ in range(5)))
    on(monkeypatch, date(2026, 10, 21))
    result = tasks.schedule_payment_consultations.apply()
    assert result.successful(), result.result
    assert result.get() == 4
    assert sweeps._pending == []
    assert len(deliveries) == 4
    with tenant_context(world.termo.tenant.pk):
        assert not PaymentQuery.objects.filter(client_id=failed_client).exists()
    fail = False
    on(monkeypatch, date(2026, 10, 22))
    result = tasks.schedule_payment_consultations.apply()
    assert result.successful(), result.result
    assert result.get() == 1
    assert sweeps._pending == []
    assert len(deliveries) == len(set(deliveries)) == 5
    assert len(world.sent()) == 5
    with tenant_context(world.termo.tenant.pk):
        assert PaymentQuery.objects.count() == 5
        assert set(PaymentQuery.objects.values_list("state", flat=True)) == {
            "nada_localizado"
        }

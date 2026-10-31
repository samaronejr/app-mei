"""AXRM-015 A6 SP-PAG cadence: the daily beat consults each client once a month.

Beat fires `schedule_payment_consultations` every day. These tests drive that real
task, and the real run task it enqueues, day after day against the loopback
`FakeSerpro`, counting the paid requests that actually reach it.
"""

from collections.abc import Callable
from datetime import date
from types import SimpleNamespace

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations.models import IntegrationIssue
from apps.integrations.serpro import tasks
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.payments import queue_payment_consultation
from tests.fakes.provider import Reply
from tests.integrations.pagtoweb_support import WINDOW, World, item, page, world

__all__ = ["world"]

pytestmark = pytest.mark.django_db(transaction=True)

Fire = Callable[[date], int]


@pytest.fixture
def fire(world: World, monkeypatch: pytest.MonkeyPatch) -> Fire:
    """Run the beat entry on `day`; the run tasks it enqueues execute in-process."""

    def run_now(tenant_id: str, query_id: str) -> object:
        return tasks.run_payment_query_task.apply(args=(tenant_id, query_id)).get()

    monkeypatch.setattr(tasks.run_payment_query_task, "delay", run_now)

    def fire_on(day: date) -> int:
        monkeypatch.setattr(tasks, "timezone", SimpleNamespace(localdate=lambda: day))
        queued: int = tasks.schedule_payment_consultations.apply().get()
        return queued

    return fire_on


def paid(world: World) -> int:
    """Consultation pages that reached the Receita: each one is billable."""
    return len(world.sent())


def _queries(world: World) -> list[tuple[str, date, date, str]]:
    with tenant_context(world.termo.tenant.pk):
        return list(
            PaymentQuery.objects.order_by("created_at", "pk").values_list(
                "trigger", "range_start", "range_end", "state"
            )
        )


def test_the_schedule_consults_once_per_month_whatever_day_it_fires(
    world: World, fire: Fire
) -> None:
    # Given the Receita answers the October and the November consultations.
    world.script(page([item("DAS000000000010")]), page([]))
    # When beat fires every day from October 20 to November 22.
    assert fire(date(2026, 10, 20)) == 0
    assert fire(date(2026, 10, 21)) == 1
    assert paid(world) == 1
    # Then later October days, after the month's consultation finished, add nothing.
    for day in (date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 31)):
        assert fire(day) == 0, day
    assert paid(world) == 1
    # And across the month boundary only November 21 opens the next month's one.
    for day in (date(2026, 11, 1), date(2026, 11, 20)):
        assert fire(day) == 0, day
    assert fire(date(2026, 11, 21)) == 1
    assert fire(date(2026, 11, 22)) == 0
    assert paid(world) == 2
    assert [call.billed for call in world.calls()] == ["sim", "sim"]
    assert _queries(world) == [
        ("agendada", date(2026, 9, 1), date(2026, 10, 21), "encontrado"),
        ("agendada", date(2026, 10, 1), date(2026, 11, 21), "nada_localizado"),
    ]


def test_after_a_definitive_failure_only_staff_pay_for_another_consultation(
    world: World, fire: Fire
) -> None:
    # Given the month's scheduled consultation ends in a billed definitive failure.
    world.script(Reply(body=b'{"status":200,"mensagens":[],"dados":"{}"}'))
    assert fire(date(2026, 10, 21)) == 1
    [(_, _, _, state)] = _queries(world)
    assert state == "falha"
    assert paid(world) == 1
    tenant_id = world.termo.tenant.pk
    with tenant_context(tenant_id):
        failed = PaymentQuery.objects.get()
        assert IntegrationIssue.objects.filter(
            reason="consulta_pagamentos_falhou"
        ).exists()
    # When beat fires on later days and the run task is redelivered.
    assert fire(date(2026, 10, 22)) == 0
    assert fire(date(2026, 10, 30)) == 0
    redelivered = tasks.run_payment_query_task.apply(
        args=(str(tenant_id), str(failed.pk))
    ).get()
    # Then nothing more reached the Receita automatically.
    assert redelivered == "falha"
    assert paid(world) == 1
    # And the deliberate staff retry is still one click and one paid request away.
    world.script(page([]))
    with tenant_context(tenant_id):
        retry = queue_payment_consultation(
            world.termo.actor,
            world.termo.client.pk,
            WINDOW,
            expected_latest=failed.pk,
            nonce="staff-retry",
        )
    assert paid(world) == 2
    assert fire(date(2026, 10, 31)) == 0
    assert paid(world) == 2
    with tenant_context(tenant_id):
        retry.refresh_from_db()
    assert (retry.trigger, retry.state) == ("equipe", "nada_localizado")

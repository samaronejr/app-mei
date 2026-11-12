"""AXRM-018's live claim guards and scheduler heartbeat boundaries."""

from collections.abc import Callable
from datetime import date, datetime
from uuid import UUID

import pytest
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations.models import ClientProviderLink, IntegrationIssue, LinkStatus
from apps.integrations.serpro import sweeps, tasks
from apps.integrations.serpro.models import PaymentQuery
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.obligations.models import SchedulerHeartbeat
from tests.integrations.pagtoweb_support import World, page, world
from tests.integrations.test_federal_sweeps import lane, on

__all__ = ["lane", "world"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_enclosing_transaction_is_refused_before_any_sweep_write_or_handoff(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliveries: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks.run_payment_query_task,
        "delay",
        lambda tenant_id, query_id: deliveries.append((tenant_id, query_id)),
    )
    on(monkeypatch, date(2026, 10, 21))
    with transaction.atomic():
        result = tasks.schedule_payment_consultations.apply()
        assert result.failed()
        assert isinstance(result.result, RuntimeError)
        assert not SchedulerHeartbeat.objects.filter(
            name="schedule_payment_consultations"
        ).exists()
    assert deliveries == []
    with tenant_context(world.termo.tenant.pk):
        assert not PaymentQuery.objects.exists()


@pytest.mark.parametrize("day", [1, 20])
def test_before_the_cadence_the_task_finishes_without_starting_a_sweep(
    world: World, monkeypatch: pytest.MonkeyPatch, day: int
) -> None:
    on(monkeypatch, date(2026, 10, day))
    result = tasks.schedule_payment_consultations.apply()
    assert result.successful(), result.result
    assert result.get() == 0
    assert not SchedulerHeartbeat.objects.filter(
        name="schedule_payment_consultations"
    ).exists()
    assert world.sent() == []
    with tenant_context(world.termo.tenant.pk):
        assert not PaymentQuery.objects.exists()


def test_attempt_and_running_stamp_precede_work_and_success_follows_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    during: list[tuple[datetime | None, str, datetime | None]] = []
    original = sweeps.payments_for

    def observe(client: ClientCompany, window: PaymentRange) -> sweeps.Outcome:
        stamp = SchedulerHeartbeat.objects.get(name="schedule_payment_consultations")
        during.append(
            (stamp.last_attempt_at, stamp.last_error_code, stamp.last_success_at)
        )
        return original(client, window)

    def deliver(tenant_id: str, query_id: str) -> object:
        result = tasks.run_payment_query_task.apply(args=(tenant_id, query_id))
        assert result.successful(), result.result
        return result.get()

    monkeypatch.setattr(sweeps, "payments_for", observe)
    monkeypatch.setattr(tasks.run_payment_query_task, "delay", deliver)
    world.script(page([]))
    before = timezone.now()
    on(monkeypatch, date(2026, 10, 21))
    result = tasks.schedule_payment_consultations.apply()
    assert result.successful(), result.result
    assert result.get() == 1
    assert len(during) == 1
    attempted, code, previous_success = during[0]
    assert attempted is not None
    assert attempted >= before
    assert code == sweeps.RUNNING
    assert previous_success is None
    stamp = SchedulerHeartbeat.objects.get(name="schedule_payment_consultations")
    assert stamp.last_attempt_at == attempted
    assert stamp.last_error_code == ""
    assert stamp.last_success_at is not None
    assert stamp.last_success_at >= attempted
    assert len(world.sent()) == 1


def test_link_withdrawn_after_selection_is_refused_without_a_failed_turn(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sweeps._pick
    deliveries: list[tuple[str, str]] = []

    def withdraw(
        tenant_id: UUID,
        field: str,
        candidates: Callable[[], QuerySet[ClientCompany]],
    ) -> tuple[UUID, list[ClientCompany]]:
        picked = original(tenant_id, field, candidates)
        with tenant_context(tenant_id):
            ClientProviderLink.objects.filter(pk=world.link.pk).update(
                status=LinkStatus.REVOGADA
            )
        return picked

    monkeypatch.setattr(sweeps, "_pick", withdraw)
    monkeypatch.setattr(
        tasks.run_payment_query_task,
        "delay",
        lambda tenant_id, query_id: deliveries.append((tenant_id, query_id)),
    )
    on(monkeypatch, date(2026, 10, 21))
    result = tasks.schedule_payment_consultations.apply()
    assert result.successful(), result.result
    assert result.get() == 0
    stamp = SchedulerHeartbeat.objects.get(name="schedule_payment_consultations")
    assert stamp.last_error_code == ""
    assert stamp.last_success_at is not None
    assert deliveries == []
    assert world.sent() == []
    with tenant_context(world.termo.tenant.pk):
        assert not PaymentQuery.objects.exists()
        assert not IntegrationIssue.objects.exists()

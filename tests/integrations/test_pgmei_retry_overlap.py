"""Retain review 3's real 401 overlap oracle and its pre-claim race variant."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.db import DatabaseError, connection, connections

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.authority import raise_problem
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.serpro import pgmei
from apps.integrations.serpro.reauthentication import (
    definitive_401,
    schedule_client_retry,
)
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    PRICE_MICROBRL,
    GuideWorld,
    das_reply,
    guide_world,
)
from tests.integrations.test_pgmei_interleavings import Runners
from tests.integrations.test_pgmei_recovery import guide_calls, links, ownership

if TYPE_CHECKING:
    from uuid import UUID

    from apps.clients.models import ClientCompany
    from apps.integrations.authority import Problem
    from apps.integrations.call_contracts import CallResult, Claim
    from apps.integrations.call_dispatch import Dispatch

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


class RetryRunners(Runners):
    """Observe real wire outcomes even when the predecessor oracle is violated."""

    def __init__(self, world: GuideWorld, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(world, monkeypatch)
        self.predecessors_at_send: list[tuple[str, list[tuple[str, str, int]]]] = []
        real_schedule = schedule_client_retry
        real_retry_claim = call_protocol.claim

        def schedule(ticket: Claim, result: CallResult) -> bool:
            if definitive_401(result):
                self.checkpoint("before-retry-schedule")
            return real_schedule(ticket, result)

        def retry_claim(tenant_id: UUID, call_id: UUID) -> Claim | None:
            ticket = real_retry_claim(tenant_id, call_id)
            if ticket is not None:
                self.checkpoint("retry-claimed")
            return ticket

        monkeypatch.setattr(call_protocol, "schedule_client_retry", schedule)
        monkeypatch.setattr(call_protocol, "claim", retry_claim)
        self.schedule_retry = schedule

    def send(self, adapter: pgmei.GuideAdapter, dispatch: Dispatch) -> CallResult:
        self.checkpoint("send")
        calls = guide_calls(self.world)
        current = next(call for call in calls if call.pk == dispatch.claim.call_id)
        earlier = [
            call
            for call in calls
            if (call.created_at, call.pk) < (current.created_at, current.pk)
        ]
        self.predecessors_at_send.append(
            (str(self.runner.name), [(str(c.pk), c.state, c.attempts) for c in earlier])
        )
        result = self.real_send(adapter, dispatch)
        self.inspected_sends.append(dispatch.claim.call_id)
        self.checkpoint("reply")
        return result


def test_owned_401_cannot_reopen_beneath_a_new_day_runner_that_passed_the_barrier(
    guide_world: GuideWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = guide_world
    runners = RetryRunners(world, monkeypatch)
    with FakeSerpro(
        (
            Reply(status=401),
            das_reply(pdf=GUIDE_PDF + b"% new-day runner B\n"),
            das_reply(pdf=GUIDE_PDF + b"% delayed retry runner A\n"),
        )
    ) as peer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runners.click, "A", peer.origin)
            try:
                runners.advance("A", runners.next_event("A"), "before-retry-schedule")
                [a] = guide_calls(world)
                assert a.state == CallState.FAILED
                assert a.attempts == 1
                runners.day += timedelta(days=1)
                # A complete 401 has been finalized, but its live protocol has not
                # scheduled its safe retry yet. The first B click settles A.
                settle = pool.submit(runners.click, "B", peer.origin)
                assert runners.next_event("B") == "done"
                settled = settle.result(timeout=30)
                assert isinstance(settled, pgmei.GuideOutcome)
                assert settled.code == "guia_nao_obtida"
                sys.stdout.write(
                    f"RV3_401_OWNED_BEFORE_RETRY {a.pk} {ownership(world, a)}\n"
                )
                # B's next deliberate click passes both predecessor checks while
                # A is final and owned, then pauses immediately before its claim.
                second = pool.submit(runners.click, "B", peer.origin)
                runners.advance("B", runners.next_event("B"), "claim")
                # Observe either the old forbidden reopen or the repaired refusal.
                # The wire/billing oracle below still runs if the retry reopens.
                runners.permits["A"].put(None)
                a_stop = runners.next_event("A")
                assert a_stop in {"retry-claimed", "done"}
                runners.advance("B", "claim")
                b_result = second.result(timeout=30)
                assert isinstance(b_result, pgmei.GuideOutcome)
                if a_stop != "done":
                    runners.advance("A", a_stop)
                a_result = first.result(timeout=30)
                assert isinstance(a_result, pgmei.GuideOutcome)
            finally:
                runners.release_all()
        calls = guide_calls(world)
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            reservations = list(
                ProviderReservation.objects.order_by(
                    "call_id", "attempt_number"
                ).values_list("call_id", "attempt_number", "state", "microbrl")
            )
        sys.stdout.write(
            f"RV3_401_REOPEN_RACE outcomes={[a_result.code, b_result.code]} "
            f"wire_sends={len(peer.requests)} "
            f"predecessors_at_send={runners.predecessors_at_send} "
            f"calls={[(c.state, c.attempts, c.fence, c.billed) for c in calls]} "
            f"known={budget.known_liability} reserved={budget.reserved} "
            f"reservations={reservations} links={links(world)}\n"
        )
        # Independent forbidden-state literal: B must never buy while its earlier
        # A call has already reopened into a committed, non-final claim.
        violations = [
            entry
            for entry in runners.predecessors_at_send
            if entry[0] == "B"
            and any(state == CallState.CLAIMED for _, state, _ in entry[1])
        ]
        assert not violations, (
            "B sent a paid guide while its already-owned predecessor was "
            "CLAIMED for retry"
        )
        assert budget.known_liability <= PRICE_MICROBRL, (
            "one 401 plus this overlap bought two billed guides"
        )
        assert a_stop == "done", "ownership retained authority to claim another retry"
        assert calls[0].state == CallState.FAILED
        assert calls[0].attempts == 1
        assert calls[0].lease_owner is None
        assert calls[0].lease_until is None
        assert len(peer.requests) == 2
        assert links(world) == 1


class ScheduledRetryRunners(RetryRunners):
    """Pause after the real retry schedule but before its claim."""

    def __init__(self, world: GuideWorld, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(world, monkeypatch)
        real_schedule = self.schedule_retry

        def schedule(ticket: Claim, result: CallResult) -> bool:
            scheduled = real_schedule(ticket, result)
            if scheduled:
                self.checkpoint("retry-scheduled")
            return scheduled

        monkeypatch.setattr(call_protocol, "schedule_client_retry", schedule)


def test_predecessor_reopens_after_barrier_before_new_claim_blocks_paid_dispatch(
    guide_world: GuideWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = guide_world
    runners = ScheduledRetryRunners(world, monkeypatch)
    with FakeSerpro(
        (
            Reply(status=401),
            das_reply(pdf=GUIDE_PDF + b"% new-day runner B\n"),
            das_reply(pdf=GUIDE_PDF + b"% delayed retry runner A\n"),
        )
    ) as peer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runners.click, "A", peer.origin)
            try:
                runners.advance("A", runners.next_event("A"), "before-retry-schedule")
                [a] = guide_calls(world)
                assert a.state == CallState.FAILED
                assert a.attempts == 1
                runners.day += timedelta(days=1)
                # A complete 401 has been finalized, but its live protocol has not
                # scheduled its safe retry yet. The first B click settles A.
                settle = pool.submit(runners.click, "B", peer.origin)
                assert runners.next_event("B") == "done"
                settled = settle.result(timeout=30)
                assert isinstance(settled, pgmei.GuideOutcome)
                assert settled.code == "guia_nao_obtida"
                sys.stdout.write(
                    f"RV3_401_OWNED_BEFORE_RETRY {a.pk} {ownership(world, a)}\n"
                )
                # B's next deliberate click passes both predecessor checks while
                # A is final and owned, then pauses immediately before its claim.
                second = pool.submit(runners.click, "B", peer.origin)
                runners.advance("B", runners.next_event("B"), "claim")
                runners.permits["A"].put(None)
                a_stop = runners.next_event("A")
                assert a_stop in {"retry-scheduled", "done"}
                runners.advance("B", "claim")
                b_result = second.result(timeout=30)
                assert isinstance(b_result, pgmei.GuideOutcome)
                if a_stop != "done":
                    runners.advance("A", a_stop)
                a_result = first.result(timeout=30)
                assert isinstance(a_result, pgmei.GuideOutcome)
            finally:
                runners.release_all()
        calls = guide_calls(world)
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            reservations = list(
                ProviderReservation.objects.order_by(
                    "call_id", "attempt_number"
                ).values_list("call_id", "attempt_number", "state", "microbrl")
            )
        sys.stdout.write(
            f"RV3_401_SCHEDULE_BEFORE_CLAIM outcomes={[a_result.code, b_result.code]} "
            f"wire_sends={len(peer.requests)} "
            f"predecessors_at_send={runners.predecessors_at_send} "
            f"calls={[(c.state, c.attempts, c.fence, c.billed) for c in calls]} "
            f"known={budget.known_liability} reserved={budget.reserved} "
            f"reservations={reservations} links={links(world)}\n"
        )
        # Independent forbidden-state literal: B must never buy while its earlier
        # A call has already reopened into a committed, non-final retry.
        violations = [
            entry
            for entry in runners.predecessors_at_send
            if entry[0] == "B"
            and any(state == CallState.RETRY for _, state, _ in entry[1])
        ]
        assert not violations, (
            "B sent a paid guide while its already-owned predecessor was RETRY "
            "before the new claim"
        )
        assert budget.known_liability <= PRICE_MICROBRL, (
            "one 401 plus this overlap bought two billed guides"
        )
        assert a_stop == "done", (
            "ownership retained authority to schedule another retry"
        )
        assert calls[0].state == CallState.FAILED
        assert calls[0].attempts == 1
        assert calls[0].lease_owner is None
        assert calls[0].lease_until is None
        assert len(peer.requests) == 2
        assert links(world) == 1


def test_owning_a_final_401_holds_the_call_lock_and_revokes_the_ticket(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = guide_world
    captured: list[tuple[Claim, CallResult]] = []
    checked: list[bool] = []
    real_problem = raise_problem
    real_schedule = schedule_client_retry

    def hold_retry(ticket: Claim, result: CallResult) -> bool:
        captured.append((ticket, result))
        return False

    def competing_call_lock(call_id: UUID) -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            with tenant_context(world.tenant_id):
                ProviderCall.objects.select_for_update(nowait=True).get(pk=call_id)
        except DatabaseError:
            return True
        else:
            return False
        finally:
            connections.close_all()

    def owned_under_lock(client: ClientCompany, problem: Problem) -> None:
        ticket, _ = captured[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            held = pool.submit(competing_call_lock, ticket.call_id).result(timeout=10)
        checked.append(held)
        assert held, "retry scheduling can acquire the call during ownership"
        real_problem(client, problem)

    with FakeSerpro((Reply(status=401), das_reply())) as peer:
        with monkeypatch.context() as patcher:
            patcher.setattr(call_protocol, "schedule_client_retry", hold_retry)
            patcher.setattr(pgmei, "raise_problem", owned_under_lock)
            result = pgmei.obtain_das_guide(
                world.owner, world.tenant_id, world.cycle.pk, origin=peer.origin
            )
        ticket, receipt = captured[0]
        [call] = guide_calls(world)
        assert result.code == "guia_nao_obtida"
        assert checked == [True]
        assert call.state == CallState.FAILED
        assert call.fence > ticket.fence
        assert call.lease_owner is None
        assert call.lease_until is None
        assert ownership(world, call)
        assert not real_schedule(ticket, receipt)
        assert call_protocol.claim(world.tenant_id, call.pk) is None
        assert pgmei.settle_guide(world.tenant_id, call.pk) == "guia_nao_obtida"
        [again] = guide_calls(world)
        assert again.fence == call.fence
        assert len(peer.requests) == 1
        assert links(world) == 0
        with tenant_context(world.tenant_id):
            assert ProviderReservation.objects.get(call=call).state == "released"
            budget = ProviderBudget.objects.get()
            assert budget.reserved == budget.known_liability == 0

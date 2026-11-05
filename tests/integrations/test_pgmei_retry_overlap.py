"""Retain review 3's real 401 overlap oracle and its pre-claim race variant."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState
from apps.integrations.serpro.reauthentication import definitive_401
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

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


class RetryRunners(Runners):
    """Observe wire outcomes without stopping the actual production send on a violation."""

    def __init__(self, world, monkeypatch):
        super().__init__(world, monkeypatch)
        self.predecessors_at_send = []
        real_schedule = call_protocol.schedule_client_retry
        real_retry_claim = call_protocol.claim

        def schedule(ticket, result):
            if definitive_401(result):
                self.checkpoint("before-retry-schedule")
            return real_schedule(ticket, result)

        def retry_claim(tenant_id, call_id):
            ticket = real_retry_claim(tenant_id, call_id)
            if ticket is not None:
                self.checkpoint("retry-claimed")
            return ticket

        monkeypatch.setattr(call_protocol, "schedule_client_retry", schedule)
        monkeypatch.setattr(call_protocol, "claim", retry_claim)

    def send(self, adapter, dispatch):
        self.checkpoint("send")
        calls = guide_calls(self.world)
        current = next(call for call in calls if call.pk == dispatch.claim.call_id)
        earlier = [call for call in calls if (call.created_at, call.pk) < (current.created_at, current.pk)]
        self.predecessors_at_send.append((str(self.runner.name), [(str(c.pk), c.state, c.attempts) for c in earlier]))
        result = self.real_send(adapter, dispatch)
        self.inspected_sends.append(dispatch.claim.call_id)
        self.checkpoint("reply")
        return result


def test_owned_401_cannot_reopen_beneath_a_new_day_runner_that_passed_the_barrier(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = guide_world
    runners = RetryRunners(world, monkeypatch)
    with FakeSerpro((
        Reply(status=401),
        das_reply(pdf=GUIDE_PDF + b"% new-day runner B\n"),
        das_reply(pdf=GUIDE_PDF + b"% delayed retry runner A\n"),
    )) as peer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runners.click, "A", peer.origin)
            try:
                runners.advance("A", runners.next_event("A"), "before-retry-schedule")
                [a] = guide_calls(world)
                assert a.state == CallState.FAILED and a.attempts == 1
                runners.day += timedelta(days=1)
                # A complete 401 has been finalized, but its live protocol has not
                # scheduled its safe retry yet. The first B click settles A.
                settle = pool.submit(runners.click, "B", peer.origin)
                assert runners.next_event("B") == "done"
                settled = settle.result(timeout=30)
                assert settled.code == "guia_nao_obtida"
                print("RV3_401_OWNED_BEFORE_RETRY", a.pk, ownership(world, a), flush=True)
                # B's next deliberate click passes both predecessor checks while
                # A is final and owned, then pauses immediately before its claim.
                second = pool.submit(runners.click, "B", peer.origin)
                runners.advance("B", runners.next_event("B"), "claim")
                # A now takes the existing production reauthentication path and
                # commits a fresh retry claim. It is non-final when B sends.
                runners.advance("A", "before-retry-schedule", "retry-claimed")
                calls = guide_calls(world)
                assert calls[0].state == CallState.CLAIMED and calls[0].fence == 2
                runners.advance("B", "claim")
                b_result = second.result(timeout=30)
                runners.advance("A", "retry-claimed")
                a_result = first.result(timeout=30)
            finally:
                runners.release_all()
        calls = guide_calls(world)
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            reservations = list(ProviderReservation.objects.order_by("call_id", "attempt_number")
                                .values_list("call_id", "attempt_number", "state", "microbrl"))
        print("RV3_401_REOPEN_RACE", "outcomes", [a_result.code, b_result.code],
              "wire_sends", len(peer.requests), "predecessors_at_send", runners.predecessors_at_send,
              "calls", [(str(c.pk), c.state, c.attempts, c.fence, c.billed) for c in calls],
              "known", budget.known_liability, "reserved", budget.reserved,
              "reservations", reservations, "links", links(world), flush=True)
        # Independent forbidden-state literal: B must never buy while its earlier
        # A call has already reopened into a committed, non-final claim.
        violations = [entry for entry in runners.predecessors_at_send
                      if entry[0] == "B" and any(state == CallState.CLAIMED for _, state, _ in entry[1])]
        assert not violations, "B sent a paid guide while its already-owned predecessor was CLAIMED for retry"
        assert budget.known_liability <= PRICE_MICROBRL, "one 401 plus this overlap bought two billed guides"


class ScheduledRetryRunners(RetryRunners):
    """Pause after the real retry schedule but before its claim."""

    def __init__(self, world, monkeypatch):
        super().__init__(world, monkeypatch)
        real_schedule = call_protocol.schedule_client_retry

        def schedule(ticket, result):
            scheduled = real_schedule(ticket, result)
            if scheduled:
                self.checkpoint("retry-scheduled")
            return scheduled

        monkeypatch.setattr(call_protocol, "schedule_client_retry", schedule)


def test_predecessor_reopens_after_barrier_before_new_claim_blocks_paid_dispatch(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = guide_world
    runners = ScheduledRetryRunners(world, monkeypatch)
    with FakeSerpro((
        Reply(status=401),
        das_reply(pdf=GUIDE_PDF + b"% new-day runner B\n"),
        das_reply(pdf=GUIDE_PDF + b"% delayed retry runner A\n"),
    )) as peer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runners.click, "A", peer.origin)
            try:
                runners.advance("A", runners.next_event("A"), "before-retry-schedule")
                [a] = guide_calls(world)
                assert a.state == CallState.FAILED and a.attempts == 1
                runners.day += timedelta(days=1)
                # A complete 401 has been finalized, but its live protocol has not
                # scheduled its safe retry yet. The first B click settles A.
                settle = pool.submit(runners.click, "B", peer.origin)
                assert runners.next_event("B") == "done"
                settled = settle.result(timeout=30)
                assert settled.code == "guia_nao_obtida"
                print("RV3_401_OWNED_BEFORE_RETRY", a.pk, ownership(world, a), flush=True)
                # B's next deliberate click passes both predecessor checks while
                # A is final and owned, then pauses immediately before its claim.
                second = pool.submit(runners.click, "B", peer.origin)
                runners.advance("B", runners.next_event("B"), "claim")
                # A now takes the existing production reauthentication path and
                # schedules its retry. It is non-final before B claims and sends.
                runners.advance("A", "before-retry-schedule", "retry-scheduled")
                calls = guide_calls(world)
                assert calls[0].state == CallState.RETRY and calls[0].fence == 1
                runners.advance("B", "claim")
                b_result = second.result(timeout=30)
                runners.advance("A", "retry-scheduled")
                a_result = first.result(timeout=30)
            finally:
                runners.release_all()
        calls = guide_calls(world)
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            reservations = list(ProviderReservation.objects.order_by("call_id", "attempt_number")
                                .values_list("call_id", "attempt_number", "state", "microbrl"))
        print("RV3_401_SCHEDULE_BEFORE_CLAIM", "outcomes", [a_result.code, b_result.code],
              "wire_sends", len(peer.requests), "predecessors_at_send", runners.predecessors_at_send,
              "calls", [(str(c.pk), c.state, c.attempts, c.fence, c.billed) for c in calls],
              "known", budget.known_liability, "reserved", budget.reserved,
              "reservations", reservations, "links", links(world), flush=True)
        # Independent forbidden-state literal: B must never buy while its earlier
        # A call has already reopened into a committed, non-final retry.
        violations = [entry for entry in runners.predecessors_at_send
                      if entry[0] == "B" and any(state == CallState.RETRY for _, state, _ in entry[1])]
        assert not violations, "B sent a paid guide while its already-owned predecessor was RETRY before the new claim"
        assert budget.known_liability <= PRICE_MICROBRL, "one 401 plus this overlap bought two billed guides"

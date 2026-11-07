"""Bounded real-protocol interleavings of two guide runners and a day change."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from queue import Queue
from threading import local
from typing import TYPE_CHECKING

import pytest
from django.db import connection, connections
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_models import CallState
from apps.integrations.serpro import pgmei
from apps.integrations.serpro.reauthentication import (
    definitive_401,
    schedule_client_retry,
)
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    GuideWorld,
    das_reply,
    guide_world,
)
from tests.integrations.test_pgmei_recovery import (
    expire_lease,
    guide_calls,
    obtain,
    sweep,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.accounts.models import User
    from apps.integrations.call_contracts import CallRequest, CallResult, Claim
    from apps.integrations.call_dispatch import Dispatch
    from apps.integrations.call_models import ProviderCall

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]

BOUNDARIES = ("reserve", "reserved", "claim", "send", "reply", "finalize", "done")


@dataclass(frozen=True)
class Schedule:
    cut: str
    change_day: bool
    expired: bool
    first_reserver: str = ""


SCHEDULES = [
    Schedule(cut, change_day, expired)
    for cut in BOUNDARIES
    for change_day in (False, True)
    for expired in (False, True)
    if not expired or cut not in {"reserve", "done"}
] + [
    Schedule("both-reserve", change_day=True, expired=False, first_reserver=first)
    for first in ("A", "B")
]


class Runners:
    """Pause the real service at protocol boundaries, never mock its outcomes."""

    def __init__(self, world: GuideWorld, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = world
        self.events: Queue[tuple[str, str]] = Queue()
        self.permits: dict[str, Queue[None]] = {"A": Queue(), "B": Queue()}
        self.runner = local()
        self.day = timezone.localdate()
        self.real_reserve, self.real_claim = call_protocol.reserve, call_protocol.claim
        self.real_send = pgmei.GuideAdapter.send
        self.real_finalize = call_protocol.finalize
        self.inspected_sends: list[UUID] = []

        def send(adapter: pgmei.GuideAdapter, dispatch: Dispatch) -> CallResult:
            return self.send(adapter, dispatch)

        monkeypatch.setattr(pgmei, "consolidation_day", lambda: self.day)
        monkeypatch.setattr(pgmei, "reserve", self.reserve)
        monkeypatch.setattr(pgmei, "claim", self.claim)
        monkeypatch.setattr(pgmei.GuideAdapter, "send", send)
        monkeypatch.setattr(call_protocol, "finalize", self.finalize)

    def checkpoint(self, point: str) -> None:
        name = str(self.runner.name)
        self.events.put((name, point))
        self.permits[name].get(timeout=30)

    def reserve(
        self, tenant_id: UUID, link_id: UUID, request: CallRequest
    ) -> ProviderCall:
        self.checkpoint("reserve")
        call = self.real_reserve(tenant_id, link_id, request)
        self.checkpoint("reserved")
        return call

    def claim(self, tenant_id: UUID, call_id: UUID) -> Claim | None:
        self.checkpoint("claim")
        return self.real_claim(tenant_id, call_id)

    def send(self, adapter: pgmei.GuideAdapter, dispatch: Dispatch) -> CallResult:
        self.checkpoint("send")
        calls = guide_calls(self.world)
        current = next(call for call in calls if call.pk == dispatch.claim.call_id)
        earlier = [
            call
            for call in calls
            if (call.created_at, call.pk) < (current.created_at, current.pk)
        ]
        assert all(pgmei.CALL_FINAL[call.state] for call in earlier), (
            "a second paid send began while its predecessor was non-final"
        )
        result = self.real_send(adapter, dispatch)
        self.inspected_sends.append(dispatch.claim.call_id)
        self.checkpoint("reply")
        return result

    def finalize(
        self, ticket: Claim, result: CallResult, keep: call_protocol.Keep | None = None
    ) -> bool:
        self.checkpoint("finalize")
        return self.real_finalize(ticket, result, keep)

    def click(self, name: str, origin: str) -> pgmei.GuideOutcome | str:
        self.runner.name = name
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
            try:
                return obtain(self.world, origin)
            except pgmei.GuideRefused as refused:
                return refused.code
        finally:
            connections.close_all()
            self.events.put((name, "done"))

    def next_event(self, name: str) -> str:
        observed, point = self.events.get(timeout=30)
        assert observed == name, (observed, point, name)
        return point

    def advance(self, name: str, point: str, stop: str = "done") -> str:
        while point != stop:
            assert point != "done", (name, stop)
            self.permits[name].put(None)
            point = self.next_event(name)
        return point

    def release_all(self) -> None:
        # An assertion in the controller must not strand a real worker.
        for permit in self.permits.values():
            for _ in range(len(BOUNDARIES)):
                permit.put(None)


@pytest.mark.parametrize("schedule", SCHEDULES)
def test_two_runners_never_buy_while_an_earlier_emission_is_non_final(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch, schedule: Schedule
) -> None:
    world = guide_world
    runners = Runners(world, monkeypatch)
    with (
        FakeSerpro(
            (das_reply(), das_reply(pdf=GUIDE_PDF + b"% second runner\n"))
        ) as peer,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(runners.click, "A", peer.origin)
        a = runners.next_event("A")
        try:
            if schedule.cut == "both-reserve":
                # Both preliminary checks see no call. Commit both reservations
                # before either rechecks: only the oldest may claim and send.
                runners.day += timedelta(days=1)
                second = pool.submit(runners.click, "B", peer.origin)
                b = runners.next_event("B")
                assert a == b == "reserve"
                winner = schedule.first_reserver
                loser = "B" if winner == "A" else "A"
                runners.advance(winner, "reserve", "reserved")
                runners.advance(loser, "reserve", "reserved")
                runners.advance(winner, "reserved", "claim")
                runners.advance(loser, "reserved")
                runners.advance(winner, "claim")
            else:
                a = runners.advance("A", a, schedule.cut)
                if schedule.change_day:
                    runners.day += timedelta(days=1)
                if schedule.expired:
                    [call] = guide_calls(world)
                    expire_lease(world, call)
                second = pool.submit(runners.click, "B", peer.origin)
                runners.advance("B", runners.next_event("B"))
                runners.advance("A", a)
            first.result(timeout=30)
            second.result(timeout=30)
            assert len(peer.requests) == len(runners.inspected_sends)
            maximum = (
                2 if schedule.cut in {"reserve", "done"} and schedule.change_day else 1
            )
            assert len(peer.requests) <= maximum, (schedule, len(peer.requests))
            if not schedule.expired:
                assert len(peer.requests) == maximum, schedule
            for call in guide_calls(world):
                if not pgmei.CALL_FINAL[call.state]:
                    expire_lease(world, call)
            sweep(world)
            assert all(pgmei.CALL_FINAL[call.state] for call in guide_calls(world))
            with tenant_context(world.tenant_id):
                assert ProviderBudget.objects.get().reserved == 0
        finally:
            runners.release_all()
    sys.stdout.write(
        f"PGMEI_ORDERING_CHECKED {schedule} bounded_orderings={len(SCHEDULES)}\n"
    )


RETRY_CUTS = (
    "first-finalized",
    "before-retry-schedule",
    "retry-scheduled",
    "retry-claimed",
    "retry-dispatch",
    "retry-reply",
    "retry-finalized",
    "settle",
)


class RetryLifecycleRunners(Runners):
    """Include the completed-401, ownership and reopen boundaries explicitly."""

    def __init__(self, world: GuideWorld, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(world, monkeypatch)
        self.real_begin = call_protocol.begin_io
        self.real_settle = pgmei.settle_guide
        self.predecessors_at_send: list[list[str]] = []
        monkeypatch.setattr(call_protocol, "schedule_client_retry", self.schedule_retry)
        monkeypatch.setattr(call_protocol, "claim", self.retry_claim)
        monkeypatch.setattr(call_protocol, "begin_io", self.begin_retry)
        monkeypatch.setattr(pgmei, "settle_guide", self.settle)

    def finalize(
        self, ticket: Claim, result: CallResult, keep: call_protocol.Keep | None = None
    ) -> bool:
        self.checkpoint("finalize")
        finalized = self.real_finalize(ticket, result, keep)
        self.checkpoint(
            "first-finalized" if definitive_401(result) else "retry-finalized"
        )
        return finalized

    def schedule_retry(self, ticket: Claim, result: CallResult) -> bool:
        if definitive_401(result):
            self.checkpoint("before-retry-schedule")
        scheduled = schedule_client_retry(ticket, result)
        if scheduled:
            self.checkpoint("retry-scheduled")
        return scheduled

    def retry_claim(self, tenant_id: UUID, call_id: UUID) -> Claim | None:
        ticket = self.real_claim(tenant_id, call_id)
        if ticket is not None:
            self.checkpoint("retry-claimed")
        return ticket

    def begin_retry(self, ticket: Claim) -> Dispatch:
        if ticket.fence > 1:
            self.checkpoint("retry-dispatch")
        return self.real_begin(ticket)

    def send(self, adapter: pgmei.GuideAdapter, dispatch: Dispatch) -> CallResult:
        self.checkpoint("send")
        calls = guide_calls(self.world)
        current = next(call for call in calls if call.pk == dispatch.claim.call_id)
        self.predecessors_at_send.append(
            [
                call.state
                for call in calls
                if (call.created_at, call.pk) < (current.created_at, current.pk)
            ]
        )
        result = self.real_send(adapter, dispatch)
        self.inspected_sends.append(dispatch.claim.call_id)
        self.checkpoint(
            "retry-reply" if dispatch.receipt.attempt_number == 2 else "reply"
        )
        return result

    def settle(
        self,
        tenant_id: UUID,
        call_id: UUID,
        *,
        cycle_id: UUID | None = None,
        actor: User | None = None,
    ) -> str:
        if self.runner.name == "A":
            self.checkpoint("settle")
        return self.real_settle(tenant_id, call_id, cycle_id=cycle_id, actor=actor)

    def release_all(self) -> None:
        for permit in self.permits.values():
            for _ in range(len(BOUNDARIES) + len(RETRY_CUTS) * 2):
                permit.put(None)


@pytest.mark.parametrize("cut", RETRY_CUTS)
@pytest.mark.parametrize("change_day", [False, True])
def test_401_finalize_own_schedule_claim_and_day_change_never_buy_two_guides(
    guide_world: GuideWorld,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
    change_day: bool,
) -> None:
    world = guide_world
    runners = RetryLifecycleRunners(world, monkeypatch)
    with FakeSerpro(
        (
            Reply(status=401),
            das_reply(pdf=GUIDE_PDF + b"% only paid guide\n"),
            das_reply(pdf=GUIDE_PDF + b"% forbidden extra guide\n"),
        )
    ) as peer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runners.click, "A", peer.origin)
            try:
                a = runners.advance("A", runners.next_event("A"), cut)
                if change_day:
                    runners.day += timedelta(days=1)
                second = pool.submit(runners.click, "B", peer.origin)
                runners.advance("B", runners.next_event("B"))
                b_result = second.result(timeout=30)
                if (
                    isinstance(b_result, pgmei.GuideOutcome)
                    and b_result.code == "guia_nao_obtida"
                ):
                    # The first B click owns A's complete, unbilled 401. Its next
                    # click can reach claim while A's old scheduler resumes.
                    later = pool.submit(runners.click, "B", peer.origin)
                    b = runners.advance("B", runners.next_event("B"), "claim")
                    runners.advance("A", a)
                    runners.advance("B", b)
                    later.result(timeout=30)
                else:
                    runners.advance("A", a)
                first.result(timeout=30)
            finally:
                runners.release_all()
        with tenant_context(world.tenant_id):
            budget = ProviderBudget.objects.get()
            assert budget.known_liability <= 320_000
            assert budget.reserved == 0
        # These literal protocol states are independent of CALL_FINAL.
        forbidden = {
            CallState.QUEUED,
            CallState.RESERVED,
            CallState.CLAIMED,
            CallState.STARTED,
            CallState.WAITING,
            CallState.RETRY,
        }
        assert not any(
            state in forbidden
            for states in runners.predecessors_at_send
            for state in states
        )
        assert len(peer.requests) <= 2
    sys.stdout.write(
        f"PGMEI_RETRY_ORDERING_CHECKED cut={cut} change_day={change_day} "
        f"retry_orderings={len(RETRY_CUTS) * 2} "
        f"total_bounded_orderings={len(SCHEDULES) + len(RETRY_CUTS) * 2}\n"
    )

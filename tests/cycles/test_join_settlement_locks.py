"""Prove the real approval, live guide and scheduled guide transaction boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from django.db import connection, transaction

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceLink
from apps.cycles.fiscal_records import FiscalDocumentReview
from apps.cycles.models import Approval, ProposalState, ServiceCycle
from apps.cycles.records import CycleEvent, CycleState, InternalNote
from apps.cycles.records import Exception as CycleException
from apps.cycles.services.cycle import add_note, transition_cycle
from apps.cycles.tasks import open_exceptions_for_tenant
from apps.integrations.models import IntegrationIssue, IssueReason
from apps.integrations.serpro import pgmei
from apps.integrations.serpro.models import FirmIntegrationIssue
from apps.integrations.serpro.work import PROC_MISSING
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT
from tests.cycles.join_guide_fixtures import (
    after_termo_expiry,
    decide,
    final_guide,
    overdue_exception,
    pending_proposal,
    review_input,
    second_cycle,
)
from tests.cycles.join_lock_fixtures import (
    CLIENT,
    CYCLE,
    TENANT,
    available,
    locks,
    ordered,
    paused_client,
    probe,
    wait,
    worker,
)
from tests.integrations.pgmei_support import GuideWorld, guide_world

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from tests.cycles.join_lock_fixtures import Result, Seam

__all__ = ["guide_world"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_approval_and_correction_on_one_cycle(guide_world: GuideWorld) -> None:
    w = guide_world
    proposal = pending_proposal(w)
    root, correct = review_input(w)
    version = w.cycle.version
    fiscal, decision = ordered(
        w.tenant_id, lambda: correct(version), lambda: decide(w, proposal)
    )
    assert (fiscal.status, decision.status) == ("committed", "committed"), (
        fiscal,
        decision,
    )
    with tenant_context(w.tenant_id):
        assert FiscalDocumentReview.objects.get(supersedes=root).pk == fiscal.value
        assert Approval.objects.get(proposal=proposal).approver_id == w.owner.pk


def test_guide_client_fence_allows_key_share_only(guide_world: GuideWorld) -> None:
    w = guide_world

    def check() -> None:
        modes = (
            available(CLIENT, w.cycle.client_id, "KEY SHARE"),
            available(CLIENT, w.cycle.client_id, "NO KEY UPDATE"),
        )
        assert modes == (True, False), f"guide client lock matrix: {modes}"

    with (
        final_guide(w) as call,
        paused_client(None, lambda: pgmei.settle_guide(w.tenant_id, call.pk), CYCLE),
    ):
        probe(w.tenant_id, check)


def test_note_precedes_guide_settlement(guide_world: GuideWorld) -> None:
    w = guide_world
    with final_guide(w) as call:
        with tenant_context(w.tenant_id):
            w.cycle.refresh_from_db()
        version = w.cycle.version
        note, guide = ordered(
            w.tenant_id,
            lambda: (
                add_note(
                    w.owner, w.cycle.pk, "Nota da competência", expected_version=version
                ).pk
            ),
            lambda: pgmei.settle_guide(w.tenant_id, call.pk),
            phased_second=True,
        )
        assert (note.status, guide.value) == ("committed", "guia_obtida"), (note, guide)
        with tenant_context(w.tenant_id):
            assert InternalNote.objects.get(cycle=w.cycle).pk == note.value
            w.cycle.refresh_from_db()
            assert w.cycle.version == version + 2


def sweep(w: GuideWorld, cycles: int = 1) -> str:
    counts = open_exceptions_for_tenant(str(w.tenant_id))
    assert counts["evaluated"] == cycles
    return "swept"


def test_first_settlement_does_not_hold_overdue_cycle(guide_world: GuideWorld) -> None:
    w = guide_world
    with final_guide(w):
        existing = overdue_exception(w)
        reached, release, opener = Event(), Event(), Event()
        entry: list[bool] = []
        settle = pgmei.settle_guide

        def observe(tid: UUID, call_id: UUID, **kwargs: Any) -> str:  # noqa: ANN401
            entry.append(connection.in_atomic_block)
            return settle(tid, call_id, **kwargs)

        def seam(execute: Seam, sql: str, *args: object) -> object:
            if entry and not reached.is_set() and locks(sql, TENANT):
                reached.set()
                wait(release)
            result = execute(sql, *args)
            if not entry and locks(sql, CYCLE):
                opener.set()
            return result

        def check() -> None:
            free = available(CYCLE, w.cycle.pk, "UPDATE")
            assert (free, entry) == (True, [False]), (
                f"settlement footprint: {free=}, {entry=}"
            )

        with (
            patch.object(pgmei, "settle_guide", observe),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            task = pool.submit(worker, None, seam, lambda: sweep(w))
            try:
                wait(reached)
                assert opener.is_set()
                probe(w.tenant_id, check)
            finally:
                release.set()
            assert task.result(SIGNAL_TIMEOUT).value == "swept"
        with tenant_context(w.tenant_id):
            assert CycleException.objects.get(cycle=w.cycle).pk == existing.pk


def _refresh_correction(
    w: GuideWorld, root: FiscalDocumentReview, correct: Callable[[int], UUID]
) -> None:
    assert not FiscalDocumentReview.objects.filter(supersedes=root).exists()
    new = correct(w.cycle.version)
    assert FiscalDocumentReview.objects.get(supersedes=root).pk == new
    events = CycleEvent.objects.filter(cycle=w.cycle, type="fiscal.document.reviewed")
    assert events.count() == 2


@pytest.mark.parametrize("operation", ["correction", "approval"])
def test_scheduled_sweep_shares_an_overdue_cycle(
    guide_world: GuideWorld, operation: str
) -> None:
    w = guide_world
    proposal = pending_proposal(w)
    root, correct = review_input(w)
    prefix = CLIENT if operation == "correction" else TENANT
    with final_guide(w):
        existing = overdue_exception(w)
        with tenant_context(w.tenant_id):
            w.cycle.refresh_from_db()
        version = w.cycle.version
        rule_seen, rule_committed, start_writer, attempted = (Event() for _ in range(4))
        entries: list[bool] = []
        settle = pgmei.settle_guide

        def observe(tid: UUID, call_id: UUID, **kwargs: Any) -> str:  # noqa: ANN401
            entries.append(connection.in_atomic_block)
            return settle(tid, call_id, **kwargs)

        def schedule(execute: Seam, sql: str, *args: object) -> object:
            if entries and not rule_committed.is_set() and locks(sql, TENANT):
                # The opener still owns the cycle. Let the other writer ask for it
                # holding its own prefix before this settlement requests controls.
                start_writer.set()
                wait(attempted)
            result = execute(sql, *args)
            if not entries and not rule_seen.is_set() and locks(sql, CYCLE):
                rule_seen.set()
                transaction.on_commit(rule_committed.set)
            if entries and rule_committed.is_set() and locks(sql, prefix):
                # Cycle released: the settlement owns the actual common prefix.
                # Its version bump must commit before the held form can proceed.
                start_writer.set()
                wait(attempted)
            return result

        def contender(execute: Seam, sql: str, *args: object) -> object:
            if locks(sql, prefix if rule_committed.is_set() else CYCLE):
                attempted.set()
            return execute(sql, *args)

        with (
            patch.object(pgmei, "settle_guide", observe),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            task = pool.submit(worker, None, schedule, lambda: sweep(w))
            try:
                wait(start_writer)
                rival = pool.submit(
                    worker,
                    w.tenant_id,
                    contender,
                    lambda: (
                        correct(version)
                        if operation == "correction"
                        else decide(w, proposal)
                    ),
                )
                a, b = task.result(2 * SIGNAL_TIMEOUT), rival.result(2 * SIGNAL_TIMEOUT)
            finally:
                attempted.set()
        assert a.pid != b.pid
        assert (a.status, b.status) == (
            "committed",
            "VersionConflict" if operation == "correction" else "committed",
        ), (a, b)
        assert (rule_seen.is_set(), entries) == (True, [False]), (
            f"settlement boundary: {entries}"
        )
        with tenant_context(w.tenant_id):
            assert CycleException.objects.get(cycle=w.cycle).pk == existing.pk
            w.cycle.refresh_from_db()
            assert w.cycle.version == version + 1
            if operation == "correction":
                _refresh_correction(w, root, correct)
            else:
                proposal.refresh_from_db()
                assert proposal.state == ProposalState.APPROVED
                assert Approval.objects.get(proposal=proposal).approver_id == w.owner.pk


def through(execute: Seam, sql: str, *args: object) -> object:
    return execute(sql, *args)


def sweep_meets_live(
    w: GuideWorld,
    call_id: UUID,
    hold: Callable[[str, object], bool],
    contended: Callable[[object], bool],
    cycles: int = 1,
) -> tuple[Result, Result]:
    """Hold the flagged sweep, holding a cycle, until live settlement, already
    holding its provider controls, asks for a contended cycle."""
    held, attempted = Event(), Event()

    def schedule(execute: Seam, sql: str, *args: object) -> object:
        result = execute(sql, *args)
        if not held.is_set() and hold(sql, args[0]):
            held.set()
            wait(attempted)
        return result

    def contender(execute: Seam, sql: str, *args: object) -> object:
        if locks(sql, CYCLE) and contended(args[0]):
            attempted.set()
        return execute(sql, *args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(worker, None, schedule, lambda: sweep(w, cycles))
        try:
            if not held.wait(SIGNAL_TIMEOUT):
                ended = task.result(SIGNAL_TIMEOUT) if task.done() else "running"
                msg = f"The sweep never held a cycle: {ended}"
                raise TimeoutError(msg)
            live = pool.submit(
                worker,
                None,
                contender,
                lambda: pgmei.settle_guide(w.tenant_id, call_id),
            )
            a, b = task.result(2 * SIGNAL_TIMEOUT), live.result(2 * SIGNAL_TIMEOUT)
        finally:
            held.set()
            attempted.set()
    assert a.pid != b.pid
    return a, b


@pytest.mark.parametrize("refused", [False, True], ids=["success", "refusal"])
def test_flagged_sweep_and_live_settlement_share_the_connection(
    guide_world: GuideWorld, refused: bool
) -> None:
    w = guide_world
    table = CycleException._meta.db_table

    def opened(sql: str, params: object) -> bool:
        # No rule applies here: the first exception written is the provider-authority
        # evaluator's, and its transaction now holds the cycle.
        return f'INSERT INTO "{table}"' in sql

    with final_guide(w, refused=refused) as call, after_termo_expiry(w):
        swept, live = sweep_meets_live(w, call.pk, opened, lambda params: True)
        outcome = "guia_nao_obtida" if refused else "guia_obtida"
        assert (swept.status, live.status, live.value) == (
            "committed",
            "committed",
            outcome,
        ), (swept, live)
        with tenant_context(w.tenant_id):
            termo = FirmIntegrationIssue.objects.filter(
                connection_id=w.link.connection_id, reason=PROC_MISSING
            )
            assert termo.count() == 1
            reasons = list(
                CycleException.objects.filter(cycle=w.cycle).values_list(
                    "reason_code", flat=True
                )
            )
            assert IssueReason.AUTORIZACAO_AUSENTE in reasons
            assert reasons.count(pgmei.GUIDE_NOT_OBTAINED) == int(refused)


def test_flagged_sweep_and_live_refusal_take_two_cycles_in_one_order(
    guide_world: GuideWorld,
) -> None:
    w = guide_world
    with final_guide(w, refused=True) as call:
        later = second_cycle(w)
        existing = overdue_exception(w, later, federal=True)
        swept, live = sweep_meets_live(
            w,
            call.pk,
            lambda sql, params: locks(sql, CYCLE),
            lambda params: str(later.pk) in str(params),
            cycles=2,
        )
        assert (swept.status, live.status, live.value) == (
            "committed",
            "committed",
            "guia_nao_obtida",
        ), (swept, live)
        with tenant_context(w.tenant_id):
            for cycle in (w.cycle, later):
                reasons = list(
                    CycleException.objects.filter(cycle=cycle).values_list(
                        "reason_code", flat=True
                    )
                )
                assert reasons.count(pgmei.GUIDE_NOT_OBTAINED) == 1, reasons
                assert IssueReason.AUTORIZACAO_AUSENTE in reasons
            overdue = CycleException.objects.filter(
                cycle=later, subject_ref=existing.subject_ref
            )
            assert overdue.get().pk == existing.pk


def close(w: GuideWorld) -> UUID:
    cycle = ServiceCycle.objects.get(pk=w.cycle.pk)
    return transition_cycle(
        w.owner, cycle.pk, CycleState.CANCELLED, expected_version=cycle.version
    ).id


def test_sweep_recovery_leaves_a_guide_linked_after_its_ids_froze(
    guide_world: GuideWorld,
) -> None:
    w = guide_world
    with final_guide(w):
        tenant = w.termo.tenant
        tenant.features = [f for f in tenant.features if f != "federal_checks"]
        tenant.save(update_fields=["features"])
        settle = pgmei.settle_guide
        live: list[Result] = []

        def stale(tid: UUID, call_id: UUID, **kwargs: Any) -> str:  # noqa: ANN401
            # The sweep froze this id; a live worker links it and the cycle closes.
            if not live:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    link = pool.submit(
                        worker, None, through, lambda: settle(tid, call_id)
                    )
                    live.append(link.result(SIGNAL_TIMEOUT))
                    closing = pool.submit(worker, tid, through, lambda: close(w))
                    live.append(closing.result(SIGNAL_TIMEOUT))
            return settle(tid, call_id, **kwargs)

        with (
            patch.object(pgmei, "settle_guide", stale),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            swept = pool.submit(worker, None, through, lambda: sweep(w)).result(
                SIGNAL_TIMEOUT
            )
        assert [r.status for r in (*live, swept)] == ["committed"] * 3, (live, swept)
        assert live[0].value == "guia_obtida"
        with tenant_context(w.tenant_id):
            work = (
                CycleEvent.objects.filter(type="federal.guide.linked").count(),
                IntegrationIssue.objects.filter(reason=pgmei.GUIDE_NOT_LINKED).count(),
                CycleException.objects.filter(
                    reason_code=pgmei.GUIDE_NOT_LINKED
                ).count(),
                EvidenceLink.objects.filter(request__cycle=w.cycle).count(),
            )
            assert work == (1, 0, 0, 1), (
                f"stale recovery: (events, issues, exceptions, links) = {work}"
            )

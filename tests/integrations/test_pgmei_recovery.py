"""AXRM-017 repair 1: a guide emission a dead worker left behind is always owned.

Review 1 (RV1-INFLIGHT-DEATH-UNOWNED, RV1-SWEEP-WINDOW-ORPHAN): a worker that dies
after SERPRO replies but before the call is final, and a final success the sweep
never reached in time, must still end linked or owned, and must never be bought
twice. Oracles adapted from review 1's probes.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from celery.exceptions import Retry
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceLink
from apps.cycles.models import Exception as CycleException
from apps.integrations import tasks
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.models import IntegrationIssue
from apps.integrations.serpro import pgmei
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Tenant
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    GuideWorld,
    das_reply,
    guide_world,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.integrations.call_contracts import CallResult
    from apps.integrations.call_dispatch import Dispatch

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


def obtain(world: GuideWorld, origin: str) -> pgmei.GuideOutcome:
    return pgmei.obtain_das_guide(
        world.owner, world.tenant_id, world.cycle.pk, origin=origin
    )


def guide_calls(world: GuideWorld) -> list[ProviderCall]:
    with tenant_context(world.tenant_id):
        return list(
            ProviderCall.objects.filter(operation="SP-DAS").order_by("created_at", "pk")
        )


def refresh(world: GuideWorld, call: ProviderCall) -> ProviderCall:
    with tenant_context(world.tenant_id):
        return ProviderCall.objects.get(pk=call.pk)


def ownership(world: GuideWorld, call: ProviderCall) -> list[str]:
    with tenant_context(world.tenant_id):
        issues = [
            f"issue:{row.reason}"
            for row in IntegrationIssue.objects.filter(
                evidence_refs__contains=[str(call.pk)]
            )
        ]
        exceptions = [
            f"exception:{row.reason_code}"
            for row in CycleException.objects.filter(subject_ref=call.pk)
        ]
    return issues + exceptions


def links(world: GuideWorld) -> int:
    with tenant_context(world.tenant_id):
        return EvidenceLink.objects.filter(request__cycle=world.cycle).count()


def guide_documents(world: GuideWorld) -> int:
    with tenant_context(world.tenant_id):
        return Document.objects.filter(
            acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER
        ).count()


def sweep(world: GuideWorld, days: int = 1) -> None:
    tenant = Tenant.objects.get(pk=world.tenant_id)
    for offset in range(days):
        with tenant_context(world.tenant_id):
            pgmei.evaluate_guide_conditions(
                tenant, timezone.localdate() + timedelta(days=offset)
            )


def expire_lease(world: GuideWorld, call: ProviderCall) -> None:
    with tenant_context(world.tenant_id):
        ProviderCall.objects.filter(pk=call.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )


def die_after_reply(*, stored: bool) -> Callable[[object, Dispatch], CallResult]:
    real = pgmei.GuideAdapter.send

    def send(self: pgmei.GuideAdapter, dispatch: Dispatch) -> CallResult:
        if stored:
            real(self, dispatch)
        else:
            self.inner.send(dispatch)
        msg = "worker killed after the SERPRO reply, before finality"
        raise SystemExit(msg)

    return send  # type: ignore[return-value]


def next_day(monkeypatch: pytest.MonkeyPatch) -> None:
    tomorrow = timezone.localdate() + timedelta(days=1)
    monkeypatch.setattr(pgmei, "consolidation_day", lambda: tomorrow)


@pytest.mark.parametrize("stored", [False, True], ids=["before-ingest", "after-ingest"])
def test_death_before_finality_is_owned_by_the_sweep_and_its_pdf_adopted(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch, *, stored: bool
) -> None:
    w = guide_world
    real = pgmei.GuideAdapter.send
    monkeypatch.setattr(pgmei.GuideAdapter, "send", die_after_reply(stored=stored))
    with FakeSerpro((das_reply(), das_reply())) as peer:
        with pytest.raises(SystemExit):
            obtain(w, peer.origin)
        monkeypatch.setattr(pgmei.GuideAdapter, "send", real)
        [first] = guide_calls(w)
        # The dead worker's lease runs out and nobody clicks again that day.
        expire_lease(w, first)
        sweep(w, days=8)
        first = refresh(w, first)
        assert len(peer.requests) == 1
    # The billed, unfinished emission is owned uncertain work, never resent.
    assert first.state == CallState.UNCERTAIN
    assert "issue:chamada_provedor_incerta" in ownership(w, first)
    # A PDF that was already stored is adopted into the cycle, not bought again.
    assert guide_documents(w) == (1 if stored else 0)
    assert links(w) == (1 if stored else 0)


def test_redelivered_task_within_the_lease_ends_owned_without_a_second_send(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CELERY_TASK_ACKS_LATE + REJECT_ON_WORKER_LOST redeliver the killed task."""
    w = guide_world
    real = pgmei.GuideAdapter.send
    monkeypatch.setattr(pgmei.GuideAdapter, "send", die_after_reply(stored=True))
    with FakeSerpro((das_reply(), das_reply())) as peer:
        monkeypatch.setattr(pgmei, "fake_origin", lambda: peer.origin)
        args = (str(w.tenant_id), str(w.owner.pk), str(w.cycle.pk))
        with pytest.raises(SystemExit):
            tasks.obtain_das_guide_task(*args)
        monkeypatch.setattr(pgmei.GuideAdapter, "send", real)
        # Redelivered at once, inside the dead worker's lease: never acknowledged
        # as "in progress"; the task asks to run again once the lease is over.
        with pytest.raises(Retry):
            tasks.obtain_das_guide_task(*args)
        [first] = guide_calls(w)
        expire_lease(w, first)
        # The retry runs on the next day: it settles this call, never a new key.
        next_day(monkeypatch)
        assert (
            "call_id" in inspect.signature(tasks.obtain_das_guide_task.run).parameters
        )
        recovered = tasks.obtain_das_guide_task(*args, call_id=str(first.pk))
        assert len(peer.requests) == 1
    first = refresh(w, first)
    assert recovered == "guia_recuperada"
    assert first.state == CallState.UNCERTAIN
    assert ownership(w, first) != []
    assert links(w) == 1
    assert len(guide_calls(w)) == 1


def test_final_success_left_unlinked_through_a_flag_off_week_is_linked(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = guide_world
    original = pgmei.settle_guide

    def die(*args: object, **kwargs: object) -> str:
        raise SystemExit

    monkeypatch.setattr(pgmei, "settle_guide", die)
    with FakeSerpro((das_reply(),)) as peer, pytest.raises(SystemExit):
        obtain(w, peer.origin)
    monkeypatch.setattr(pgmei, "settle_guide", original)
    [call] = guide_calls(w)
    # Rollout recovery = flag off: the firm turns federal_checks off for a week.
    tenant = Tenant.objects.get(pk=w.tenant_id)
    features = list(tenant.features)
    tenant.features = [f for f in features if f != "federal_checks"]
    tenant.save(update_fields=["features"])
    with tenant_context(w.tenant_id):
        ProviderCall.objects.filter(pk=call.pk).update(
            updated_at=timezone.now() - timedelta(days=30)
        )
    sweep(w, days=3)
    # Settling an emission already billed does not wait for the flag or a window.
    assert links(w) == 1
    tenant.features = features
    tenant.save(update_fields=["features"])
    sweep(w, days=2)
    assert links(w) == 1
    assert refresh(w, call).state == CallState.SUCCESS


def test_next_day_click_recovers_an_unsettled_earlier_emission_instead_of_paying(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = guide_world
    real = pgmei.GuideAdapter.send
    monkeypatch.setattr(pgmei.GuideAdapter, "send", die_after_reply(stored=True))
    with FakeSerpro((das_reply(), das_reply(pdf=GUIDE_PDF + b"% next day\n"))) as peer:
        with pytest.raises(SystemExit):
            obtain(w, peer.origin)
        monkeypatch.setattr(pgmei.GuideAdapter, "send", real)
        [first] = guide_calls(w)
        expire_lease(w, first)
        # No sweep ran. The next day staff click again.
        next_day(monkeypatch)
        outcome = obtain(w, peer.origin)
        assert len(peer.requests) == 1
    assert outcome.code == "guia_recuperada"
    assert outcome.call_id == first.pk
    assert outcome.link_id is not None
    assert refresh(w, first).state == CallState.UNCERTAIN
    assert ownership(w, first) != []
    assert len(guide_calls(w)) == 1


def test_next_day_click_owns_an_earlier_emission_without_pdf_then_a_new_click_pays(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = guide_world
    real = pgmei.GuideAdapter.send
    monkeypatch.setattr(pgmei.GuideAdapter, "send", die_after_reply(stored=False))
    with FakeSerpro((das_reply(), das_reply())) as peer:
        with pytest.raises(SystemExit):
            obtain(w, peer.origin)
        monkeypatch.setattr(pgmei.GuideAdapter, "send", real)
        [first] = guide_calls(w)
        expire_lease(w, first)
        next_day(monkeypatch)
        # The click that finds the unsettled emission only settles it.
        settled = obtain(w, peer.origin)
        assert len(peer.requests) == 1
        assert settled.code == "resultado_incerto"
        assert settled.call_id == first.pk
        assert ownership(w, first) != []
        # Once that emission is owned, a deliberate new click may buy a guide.
        bought = obtain(w, peer.origin)
        assert len(peer.requests) == 2
    assert bought.code == "guia_obtida"
    assert bought.call_id != first.pk


def test_next_day_click_while_an_earlier_emission_is_live_sends_nothing(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = guide_world
    real = pgmei.GuideAdapter.send
    monkeypatch.setattr(pgmei.GuideAdapter, "send", die_after_reply(stored=False))
    with FakeSerpro((das_reply(), das_reply())) as peer:
        with pytest.raises(SystemExit):
            obtain(w, peer.origin)
        monkeypatch.setattr(pgmei.GuideAdapter, "send", real)
        [first] = guide_calls(w)
        # Its lease is still live (another worker may still be finishing it).
        next_day(monkeypatch)
        outcome = obtain(w, peer.origin)
        assert len(peer.requests) == 1
    assert outcome.code == "em_andamento"
    assert outcome.call_id == first.pk
    assert len(guide_calls(w)) == 1


def test_sweep_owns_a_claim_whose_worker_died_before_sending(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = guide_world

    def die(*args: object, **kwargs: object) -> None:
        msg = "worker killed between claim and dispatch"
        raise SystemExit(msg)

    monkeypatch.setattr(pgmei, "perform", die)
    with FakeSerpro((das_reply(),)) as peer:
        with pytest.raises(SystemExit):
            obtain(w, peer.origin)
        assert peer.requests == []
    [call] = guide_calls(w)
    expire_lease(w, call)
    sweep(w)
    call = refresh(w, call)
    with tenant_context(w.tenant_id):
        reserved = ProviderBudget.objects.get().reserved
    # Nothing was sent: the held reservation is released and the call is owned.
    assert call.state == CallState.UNCERTAIN
    assert reserved == 0
    assert ownership(w, call) != []

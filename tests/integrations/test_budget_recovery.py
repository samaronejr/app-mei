"""Accounting survives dispatch refusal, repeated replay and a month boundary."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from django.db import connections
from django.test import override_settings
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations import provider_journal
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import AdmissionDeniedError, CallResult
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import (
    Dispatch,
    begin_io,
    claim,
    finalize,
    perform,
    record_result,
    reserve,
)
from apps.integrations.firm_calls import FirmDispatch
from apps.integrations.models import ClientProviderLink
from apps.integrations.provider_replay import reconcile
from apps.tenants.models import UsageKind, UsageRecord
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_budget_caps import budget_link, paid_request, provider_link

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["budget_link", "provider_link"]


class BilledRetryPeer(ProviderPeer):
    """A read the adapter schedules for retry although its status was billed."""

    def send(self, dispatch: Dispatch | FirmDispatch) -> CallResult:
        return replace(super().send(dispatch), state=CallState.RETRY)


def balances(link: ClientProviderLink) -> tuple[tuple[int, int, int], list[str]]:
    with tenant_context(link.tenant_id):
        budget = ProviderBudget.objects.get()
        return (
            (budget.known_liability, budget.uncertain_exposure, budget.reserved),
            list(
                ProviderReservation.objects.order_by("attempt_number").values_list(
                    "state", flat=True
                )
            ),
        )


def test_restored_unbilled_read_retry_keeps_its_released_amount(
    budget_link: ClientProviderLink,
) -> None:
    # Given a read SERPRO answered with 503: known not billed, retry scheduled.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    with ProviderPeer(status=503) as peer:
        perform(ticket, peer)
    assert balances(budget_link) == ((0, 0, 0), ["released"])
    # When restore reconciliation quarantines the pending retry (twice).
    with override_settings(PROVIDERS_PAUSED=True):
        reconcile(uuid4())
        reconcile(uuid4())
    # Then the call needs review, but the unbilled amount never returns as exposure.
    assert balances(budget_link) == ((0, 0, 0), ["released"])
    with tenant_context(budget_link.tenant_id):
        assert ProviderCall.objects.get().state == CallState.UNCERTAIN


def test_restored_billed_read_retry_keeps_its_known_amount(
    budget_link: ClientProviderLink,
) -> None:
    # Given a read whose billed 202 the adapter scheduled for another attempt.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    with BilledRetryPeer(status=202) as peer:
        perform(ticket, peer)
    assert balances(budget_link) == ((240_000, 0, 0), ["known"])
    # When restore reconciliation quarantines that pending retry.
    with override_settings(PROVIDERS_PAUSED=True):
        reconcile(uuid4())
    # Then confirmed liability stays known; it is not demoted to uncertain.
    assert balances(budget_link) == ((240_000, 0, 0), ["known"])
    with tenant_context(budget_link.tenant_id):
        assert ProviderCall.objects.get().state == CallState.UNCERTAIN


def test_dispatch_refusal_releases_only_unsent_reservation(
    budget_link: ClientProviderLink, settings: SettingsWrapper
) -> None:
    # Given a valid claim followed by a provider pause.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    settings.PROVIDERS_PAUSED = True
    # When dispatch rechecks live controls.
    with pytest.raises(AdmissionDeniedError):
        begin_io(ticket)
    # Then no journal/start exists and the unused reservation is released.
    assert provider_journal.read(budget_link.tenant_id) == []
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0
        assert ProviderReservation.objects.get().state == "released"


def test_caps_lowered_between_claim_and_dispatch_refuse_io(
    budget_link: ClientProviderLink,
) -> None:
    # Given a reservation and a later owner reduction of the monthly cap.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    with tenant_context(budget_link.tenant_id):
        ProviderBudget.objects.update(cap_microbrl=100_000)
    # When the dispatch-start transaction rechecks the ceiling.
    with pytest.raises(AdmissionDeniedError):
        begin_io(ticket)
    # Then I/O never began and the previously held funds were released.
    assert provider_journal.read(budget_link.tenant_id) == []
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0


def test_start_only_replay_retains_then_reconciles_original_exposure(
    budget_link: ClientProviderLink,
) -> None:
    # Given a journaled start with a lost response/finalize.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    # When recovery sees the start twice, then a provider result is recovered.
    with override_settings(PROVIDERS_PAUSED=True):
        assert reconcile(uuid4()) == 1
        assert reconcile(uuid4()) == 0
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.reserved, budget.uncertain_exposure) == (0, 240_000)
    provider_journal.append(
        replace(
            dispatch.receipt,
            result=CallResult(
                CallState.DENIED, 403, "sem_procuracao", "sim", settled_microbrl=240_000
            ),
        )
    )
    with override_settings(PROVIDERS_PAUSED=True):
        assert reconcile(uuid4()) == 1
        assert reconcile(uuid4()) == 0
    # Then the same exposure moves to known once and usage is corrected append-only.
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.known_liability, budget.uncertain_exposure, budget.reserved) == (
            240_000,
            0,
            0,
        )
        original = UsageRecord.objects.get(source_event=call.pk)
        correction = UsageRecord.objects.get(correction_of=original)
        assert (original.kind, correction.kind, correction.quantity) == (
            "estimated",
            "confirmed",
            1,
        )


def test_retry_gets_an_independent_reservation_and_usage_source(
    budget_link: ClientProviderLink,
) -> None:
    # Given an unbilled read response eligible for retry.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    first = claim(budget_link.tenant_id, call.pk)
    assert first is not None
    with ProviderPeer(status=503) as peer:
        perform(first, peer)
    with tenant_context(budget_link.tenant_id):
        ProviderCall.objects.filter(pk=call.pk).update(next_attempt_at=timezone.now())
    # When another actual transmission is claimed and billed.
    second = claim(budget_link.tenant_id, call.pk)
    assert second is not None
    with ProviderPeer(status=200) as peer:
        perform(second, peer)
    # Then first and second reservations cannot collapse into one double-settlement.
    with tenant_context(budget_link.tenant_id):
        assert list(
            ProviderReservation.objects.order_by("attempt_number").values_list(
                "state", flat=True
            )
        ) == ["released", "known"]
        assert ProviderBudget.objects.get().known_liability == 240_000
        assert UsageRecord.objects.get(metric="serpro_consulta").quantity == 1


def test_stale_pre_send_lease_releases_but_never_resends(
    budget_link: ClientProviderLink,
) -> None:
    # Given a claimed lease that expires without dispatch starting.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    assert claim(budget_link.tenant_id, call.pk) is not None
    with tenant_context(budget_link.tenant_id):
        ProviderCall.objects.filter(pk=call.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    # When another worker tries to claim it.
    assert claim(budget_link.tenant_id, call.pk) is None
    # Then no paid work was transmitted and no reservation leaks forever.
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0


def consulta_usage(link: ClientProviderLink) -> list[tuple[str, int]]:
    with tenant_context(link.tenant_id):
        return sorted(
            UsageRecord.objects.filter(metric="serpro_consulta").values_list(
                "kind", "quantity"
            )
        )


def test_late_billed_reply_racing_reconciles_confirms_budget_usage_once(
    budget_link: ClientProviderLink, settings: SettingsWrapper
) -> None:
    # Given a paid read whose start alone was journaled, quarantined by a replay.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    settings.PROVIDERS_PAUSED = True
    reconcile(uuid4())
    assert balances(budget_link) == ((0, 240_000, 0), ["uncertain"])
    assert consulta_usage(budget_link) == [(UsageKind.ESTIMATED, 1)]
    # When the original worker's billed reply races two late finalizes and two
    # reconciles in four PostgreSQL sessions, followed by two more reconciles.
    reply = CallResult(CallState.SUCCESS, 200, "ok", "sim")
    record_result(dispatch, reply)
    barrier = Barrier(4, timeout=10)

    def race(step: Callable[[], object]) -> object:
        try:
            barrier.wait()
            return step()
        finally:
            connections.close_all()

    steps: list[Callable[[], object]] = [
        lambda: finalize(ticket, reply),
        lambda: finalize(ticket, reply),
        lambda: reconcile(uuid4()),
        lambda: reconcile(uuid4()),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(race, step) for step in steps]
        outcomes = [future.result(timeout=30) for future in futures]
    reconcile(uuid4())
    reconcile(uuid4())
    # Then the superseded replies never finalize, and the attempt is metered and
    # settled exactly once under 010's call-keyed budget usage.
    assert outcomes[:2] == [False, False]
    assert consulta_usage(budget_link) == [
        (UsageKind.CONFIRMED, 1),
        (UsageKind.ESTIMATED, 1),
    ]
    assert balances(budget_link) == ((240_000, 0, 0), ["known"])
    with tenant_context(budget_link.tenant_id):
        assert ProviderCall.objects.get().state == CallState.SUCCESS

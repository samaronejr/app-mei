"""Concurrency, changed admission and effect identity witnesses for AXRM-009."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import Final
from uuid import UUID

import pytest
from django.db import connection, connections
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts, call_protocol
from apps.integrations.budget import BudgetPolicy
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallResult,
    StaleClaimError,
)
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.integrations.serpro.work import CLIENT_ABSENT
from apps.tenants.models import Tenant
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import (
    SyntheticPolicy,
    provider_link,
    reload_call,
    request,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


def test_simultaneous_reservations_and_claims_have_one_winner(
    provider_link: ClientProviderLink,
) -> None:
    # Given two runtime sessions released at the same explicit rendezvous.
    ready = Barrier(2, timeout=10)

    def work() -> tuple[UUID, bool]:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '10s'")
            ready.wait()
            row = call_protocol.reserve(
                provider_link.tenant_id, provider_link.pk, request()
            )
            ticket = call_protocol.claim(provider_link.tenant_id, row.pk)
            return row.pk, ticket is not None
        finally:
            connections.close_all()

    # When both try the same deterministic request.
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(work) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    # Then one call, one reservation and one lease exist.
    assert results[0][0] == results[1][0]
    assert sum(won for _, won in results) == 1
    assert reload_call(provider_link, results[0][0]).reserved_microbrl == 320_000


def test_business_effect_barrier_blocks_a_second_request_identity(
    provider_link: ClientProviderLink,
) -> None:
    # Given a completed effect and a different request key for the same effect.
    row = call_protocol.reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = call_protocol.claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        call_protocol.perform(ticket, peer)
        duplicate = replace(
            request("different"), business_effect_key=request().business_effect_key
        )
        # When the same effect is presented under a new request key.
        second = call_protocol.reserve(
            provider_link.tenant_id, provider_link.pk, duplicate
        )
        next_ticket = call_protocol.claim(provider_link.tenant_id, second.pk)
        if next_ticket is not None:
            with pytest.raises((AdmissionDeniedError, StaleClaimError)):
                call_protocol.perform(next_ticket, peer)
        # Then the peer has received only the first effect.
        assert peer.requests == [ticket.owner.hex]


@pytest.mark.parametrize("changed", ["flag", "connection", "link", "authority"])
def test_dispatch_uses_live_controls_not_claim_time_snapshots(
    provider_link: ClientProviderLink, changed: str
) -> None:
    # Given a committed lease, followed by an admission change.
    row = call_protocol.reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = call_protocol.claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with tenant_context(provider_link.tenant_id):
        match changed:
            case "flag":
                Tenant.objects.filter(pk=provider_link.tenant_id).update(features=[])
            case "connection":
                ProviderConnection.objects.filter(
                    pk=provider_link.connection_id
                ).update(version=2)
            case "link":
                ClientProviderLink.objects.filter(pk=provider_link.pk).update(version=2)
            case "authority":
                policy = call_contracts.operation_for("SP-DAS").policy
                assert isinstance(policy, SyntheticPolicy)
                policy.allowed = False
    # When dispatch rechecks the controls.
    with ProviderPeer() as peer, pytest.raises(AdmissionDeniedError):
        call_protocol.perform(ticket, peer)
    # Then no external request was made.
    assert peer.requests == []


def test_late_reply_is_recorded_without_regressing_a_terminal_call(
    provider_link: ClientProviderLink,
) -> None:
    # Given a call already finalized by its current worker.
    row = call_protocol.reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = call_protocol.claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        call_protocol.perform(ticket, peer)
    # When an obsolete fence reports another result.
    stale = replace(ticket, fence=ticket.fence - 1)
    assert not call_protocol.finalize(
        stale, CallResult(CallState.FAILED, 500, "late", "nao")
    )
    # Then success remains and the late observation is retained separately.
    assert reload_call(provider_link, row.pk).state == CallState.SUCCESS
    with tenant_context(provider_link.tenant_id):
        assert ProviderCallAttempt.objects.filter(call=row, observation="late").exists()


@pytest.mark.parametrize("reread", ["raises", "names-nothing", "writes-and-returns"])
def test_a_moved_version_is_refused_whatever_the_reread_does(
    provider_link: ClientProviderLink, monkeypatch: pytest.MonkeyPatch, reread: str
) -> None:
    # Given a committed lease, then the connection version moves before dispatch.
    row = call_protocol.reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = call_protocol.claim(provider_link.tenant_id, row.pk)
    assert ticket is not None

    def failing(_call: object) -> bool:
        message = "the re-read failed"
        raise RuntimeError(message)

    def writes(_call: object) -> bool:
        ClientProviderLink.objects.filter(pk=provider_link.pk).update(
            provider_company_ref="probe-wrote"
        )
        return True

    if reread == "raises":
        monkeypatch.setattr(SyntheticPolicy, "authority", property(lambda _: failing))
    if reread == "writes-and-returns":
        monkeypatch.setattr(SyntheticPolicy, "authority", property(lambda _: writes))
    with tenant_context(provider_link.tenant_id):
        ProviderConnection.objects.filter(pk=provider_link.connection_id).update(
            version=2
        )
    # When dispatch rechecks: the re-read may only choose the cause (AXRM-045).
    with ProviderPeer() as peer, pytest.raises(AdmissionDeniedError) as refused:
        call_protocol.perform(ticket, peer)
    # Then it is a recorded authority refusal naming no cause (its state code,
    # never blank), and nothing was sent.
    assert (str(refused.value), refused.value.reason) == (
        CallState.AUTHORITY,
        CallState.AUTHORITY,
    )
    assert peer.requests == []
    assert reload_call(provider_link, row.pk).state == CallState.AUTHORITY
    # And the re-read only named a cause: none of its writes outlived it.
    with tenant_context(provider_link.tenant_id):
        kept = ClientProviderLink.objects.get(pk=provider_link.pk).provider_company_ref
    assert kept == "synthetic-company"


def _priced(
    provider_link: ClientProviderLink,
    settings: SettingsWrapper,
    authority: Callable[[ProviderCall], bool],
) -> None:
    """Admit SP-DAS under the real budget policy, so a reservation holds money."""
    settings.USAGE_UNIT_PRICES_BRL = {"serpro_emissao": "0.32"}
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "10.00"}
    call_contracts._OPERATIONS["SP-DAS"] = replace(
        call_contracts.operation_for("SP-DAS"), policy=BudgetPolicy(authority)
    )
    with tenant_context(provider_link.tenant_id):
        ProviderBudget.objects.create(
            tenant_id=provider_link.tenant_id,
            provider="serpro",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=2_000_000,
            tariff_revision="synthetic-tariff-v1",
        )


def _held(
    provider_link: ClientProviderLink, call_id: UUID
) -> tuple[list[tuple[int, str]], list[tuple[int, int, int]]]:
    """The call's slots, and every budget's reserved, known and uncertain money."""
    with tenant_context(provider_link.tenant_id):
        slots = ProviderReservation.objects.filter(call_id=call_id)
        budgets = ProviderBudget.objects.order_by("pk")
        return (
            sorted(slots.values_list("attempt_number", "state")),
            list(
                budgets.values_list("reserved", "known_liability", "uncertain_exposure")
            ),
        )


def test_a_version_moved_before_claim_is_recorded_and_released_at_claim(
    provider_link: ClientProviderLink, settings: SettingsWrapper
) -> None:
    # Given a call reserved under the real budget policy, its priced slot held...
    _priced(provider_link, settings, lambda _call: True)
    row = call_protocol.reserve(provider_link.tenant_id, provider_link.pk, request())
    assert _held(provider_link, row.pk) == ([(1, "reserved")], [(320_000, 0, 0)])
    # ...then its connection version moves before the claim.
    with tenant_context(provider_link.tenant_id):
        ProviderConnection.objects.filter(pk=provider_link.connection_id).update(
            version=2
        )
    # When it is claimed, the refusal still reaches the caller...
    with pytest.raises(AdmissionDeniedError) as refused:
        call_protocol.claim(provider_link.tenant_id, row.pk)
    # ...and the call is a recorded refusal, never left reserved (AXRM-045).
    call = reload_call(provider_link, row.pk)
    assert call.state == CallState.AUTHORITY
    # It is the typed moved refusal, naming no cause since the authority holds,
    # recorded with nothing sent and its one slot released.
    assert (type(refused.value).__name__, str(refused.value), refused.value.reason) == (
        "VersionMovedError",
        CallState.AUTHORITY,
        CallState.AUTHORITY,
    )
    assert (call.provider_code, call.attempts) == (CallState.AUTHORITY, 0)
    assert _held(provider_link, row.pk) == ([(1, "released")], [(0, 0, 0)])


# Each ordinary claim veto: the refusal it raises, and the reason it carries.
VETOES: Final = {
    "pause": (CallState.PAUSED, CallState.PAUSED),
    "authority": (CallState.AUTHORITY, CallState.AUTHORITY),
    "authority-named": (CallState.AUTHORITY, CLIENT_ABSENT),
    "link-disabled": (CallState.PAUSED, CallState.PAUSED),
    # The pause check runs before the version check (`call_state.py:57-81`).
    "moved-and-paused": (CallState.PAUSED, CallState.PAUSED),
}


@pytest.mark.parametrize("veto", list(VETOES))
def test_a_pause_at_claim_rolls_back_and_holds_nothing(
    provider_link: ClientProviderLink, settings: SettingsWrapper, veto: str
) -> None:
    # Given a call reserved under the real budget policy, so its slot and the
    # budget's reserved balance exist before the claim...
    vetoed: list[bool] = []

    def authority(_call: ProviderCall) -> bool:
        if vetoed and veto == "authority-named":
            raise AdmissionDeniedError(CallState.AUTHORITY, reason=CLIENT_ABSENT)
        return not (vetoed and veto == "authority")

    _priced(provider_link, settings, authority)
    row = call_protocol.reserve(provider_link.tenant_id, provider_link.pk, request())
    before = _held(provider_link, row.pk)
    assert before == ([(1, "reserved")], [(320_000, 0, 0)])
    # When an ordinary veto, never a version move alone, meets the claim.
    vetoed.append(True)
    with tenant_context(provider_link.tenant_id):
        if veto == "link-disabled":
            ClientProviderLink.objects.filter(pk=provider_link.pk).update(
                status="revogada"
            )
        if veto == "moved-and-paused":
            ProviderConnection.objects.filter(pk=provider_link.connection_id).update(
                version=2
            )
    if veto in {"pause", "moved-and-paused"}:
        settings.PROVIDERS_PAUSED = True
    with pytest.raises(AdmissionDeniedError) as refused:
        call_protocol.claim(provider_link.tenant_id, row.pk)
    # Then that plain refusal reaches the caller and nothing changed: only a
    # permanent refusal is recorded at claim (AXRM-045).
    assert reload_call(provider_link, row.pk).state == CallState.RESERVED
    assert (type(refused.value).__name__, str(refused.value), refused.value.reason) == (
        "AdmissionDeniedError",
        *VETOES[veto],
    )
    assert _held(provider_link, row.pk) == before

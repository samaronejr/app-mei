"""Concurrency, changed admission and effect identity witnesses for AXRM-009."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID

import pytest
from django.db import connection, connections

from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts, call_protocol
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallResult,
    StaleClaimError,
)
from apps.integrations.call_models import CallState, ProviderCallAttempt
from apps.integrations.models import ClientProviderLink, ProviderConnection
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

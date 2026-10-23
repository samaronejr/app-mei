"""Bounded read retries and excluded firm/session operations."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import Operation, register_operation
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.models import ClientProviderLink
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import (
    SyntheticPolicy,
    provider_link,
    reload_call,
    request,
)

__all__ = ["provider_link"]


@pytest.mark.parametrize("code", ["SP-AUTH", "SP-TERMO"])
def test_session_and_firm_operations_cannot_be_client_call_rows(code: str) -> None:
    # Given an operation whose scope is not a client call.
    operation = Operation(
        code,
        "serpro",
        "v1",
        "registration",
        "federal_checks",
        "emissao",
        SyntheticPolicy(),
    )
    # When an adapter tries to register it in the client protocol.
    with pytest.raises(ValueError, match="cannot be registered"):
        register_operation(operation)
    # Then no registration exists through this entry point.


@pytest.mark.django_db(transaction=True)
def test_known_read_failures_have_three_transmissions_and_bounded_backoff(
    provider_link: ClientProviderLink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a billed read and a wire peer returning definite temporary failures.
    clock = datetime(2026, 10, 23, 12, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: clock)
    register_operation(
        Operation(
            "SP-PAG",
            "serpro",
            "v1",
            "read",
            "federal_checks",
            "consulta",
            SyntheticPolicy(),
        )
    )
    row = reserve(
        provider_link.tenant_id,
        provider_link.pk,
        replace(request(), operation="SP-PAG"),
    )
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(next_attempt_at=clock)
    with ProviderPeer(status=503) as peer:
        # When each scheduled retry becomes due (no sleep or wall-clock race).
        for expected_attempt in range(1, 4):
            ticket = claim(provider_link.tenant_id, row.pk)
            assert ticket is not None
            before = timezone.now()
            result = perform(ticket, peer)
            current = reload_call(provider_link, row.pk)
            assert current.attempts == expected_attempt
            if expected_attempt < 3:
                assert result.state == CallState.RETRY
                assert current.next_attempt_at == before + timedelta(
                    seconds=60 if expected_attempt == 1 else 300
                )
                assert claim(provider_link.tenant_id, row.pk) is None
                with tenant_context(provider_link.tenant_id):
                    ProviderCall.objects.filter(pk=row.pk).update(
                        next_attempt_at=before
                    )
            else:
                assert result.state == CallState.FAILED
        # Then exhaustion is terminal and cannot produce a fourth transmission.
        assert reload_call(provider_link, row.pk).state == CallState.FAILED
        assert claim(provider_link.tenant_id, row.pk) is None
        assert len(peer.requests) == 3

"""Provider bytes leave only inside an issued AXRM-009 dispatch (A7.4, IS-5)."""

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import urllib3.util.connection

from apps.integrations import token_broker
from apps.integrations.call_contracts import CallResult, Claim, StaleClaimError
from apps.integrations.call_dispatch import Dispatch, transmit
from apps.integrations.journal_schema import JournalRecord
from apps.integrations.token_broker import TokenBrokerClient
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import (
    EnvioNaoAutorizado,
    Environment,
    OperacaoNaoPermitida,
    ProviderRequest,
    TransportFailure,
)
from tests.fakes.focus import FakeFocus
from tests.fakes.provider import Reply, wire_window
from tests.fakes.serpro import FakeSerpro

KEY = "0" * 44
CIENCIA = ProviderRequest(subject=KEY, body=b'{"tipo":"ciencia"}')
TOKENS = (
    b'{"access_token":"synthetic-access","jwt_token":"synthetic-jwt","expires_in":2008}'
)


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, dispatch: Dispatch) -> CallResult:
        self.calls += 1
        return CallResult("sucesso", 200, "ok", "nao")


def fabricated_dispatch() -> Dispatch:
    """Every field is well-formed, but no begin_io() ever issued it."""
    ticket = Claim(uuid4(), uuid4(), uuid4(), 1, uuid4(), 1)
    receipt = JournalRecord(
        tenant_id=ticket.tenant_id,
        client_id=ticket.client_id,
        link_id=uuid4(),
        call_id=ticket.call_id,
        attempt_id=ticket.owner,
        attempt_number=1,
        fence=ticket.fence,
        execution_epoch=ticket.execution_epoch,
        operation="FC-CIE",
        operation_revision="v1",
        operation_class="registration",
        provider="focus",
        environment="producao",
        request_key="a" * 64,
        request_digest="b" * 64,
        business_effect_key="c" * 64,
        billing_class="incluido_no_plano",
        reserved_microbrl=0,
        tariff_revision="synthetic",
        occurred_at=datetime.now(UTC),
    )
    return Dispatch(ticket, receipt, journaled=True)


def test_registration_without_a_dispatch_never_reaches_the_peer() -> None:
    # Given a well-formed, allowlisted Ciencia registration and a live peer.
    with FakeFocus((Reply(),)) as peer:
        # When it is sent directly through the public transport API.
        refused = False
        try:
            ProviderTransport("focus", "fake", peer.origin).send("FC-CIE", CIENCIA)
        except OperacaoNaoPermitida:
            refused = True
        # Then no request reaches the peer and the call is refused.
        assert peer.requests == []
        assert refused


@pytest.mark.parametrize("environment", ["homologacao", "producao"])
def test_real_environment_without_a_dispatch_opens_no_connection(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given every outbound connection is recorded and never really opened.
    attempts: list[tuple[str, int]] = []

    def record(address: tuple[str, int], *args: object, **kwargs: object) -> None:
        attempts.append(address)
        msg = "synthetic: tests never reach a real provider"
        raise OSError(msg)

    monkeypatch.setattr(urllib3.util.connection, "create_connection", record)
    # When a real-origin registration is sent outside any dispatch.
    with pytest.raises((OperacaoNaoPermitida, TransportFailure)) as caught:
        ProviderTransport("focus", environment).send("FC-CIE", CIENCIA)
    # Then not even a connection is attempted.
    assert attempts == []
    assert isinstance(caught.value, OperacaoNaoPermitida)


def test_fabricated_dispatch_cannot_open_the_wire() -> None:
    # Given a dispatch that no begin_io() issued.
    adapter = RecordingAdapter()
    # When it is handed to the protocol's wire phase.
    refused = False
    try:
        transmit(fabricated_dispatch(), adapter)
    except StaleClaimError:
        refused = True
    # Then the adapter never runs.
    assert adapter.calls == 0
    assert refused


def test_broker_tokens_require_an_open_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a broker peer that would issue tokens.
    with FakeSerpro((Reply(body=TOKENS),)) as peer:
        monkeypatch.setattr(token_broker, "BROKER_URL", peer.origin + "/token")
        # When tokens are requested outside a dispatch.
        refused = False
        try:
            TokenBrokerClient("synthetic-hmac-secret").tokens("ABCDEF12345678")
        except OperacaoNaoPermitida:
            refused = True
        # Then the broker is never contacted.
        assert peer.requests == []
        assert refused


@pytest.mark.parametrize(
    ("provider", "environment", "operation"),
    [
        ("focus", "fake", "FC-NFE"),
        ("focus", "producao", "FC-CIE"),
        ("serpro", "fake", "FC-CIE"),
    ],
    ids=["other-operation", "other-environment", "other-provider"],
)
def test_window_for_another_call_cannot_send_ciencia(
    provider: str, environment: str, operation: str
) -> None:
    # Given a dispatch window opened for a different call.
    with FakeFocus((Reply(),)) as peer, wire_window(provider, environment, operation):
        # When Ciencia is sent through it.
        with pytest.raises(EnvioNaoAutorizado):
            ProviderTransport("focus", "fake", peer.origin).send("FC-CIE", CIENCIA)
        # Then nothing reaches the peer.
        assert peer.requests == []


def test_one_window_carries_exactly_one_transmission() -> None:
    # Given a window for one Ciencia registration.
    with FakeFocus((Reply(), Reply())) as peer, wire_window("focus", "fake", "FC-CIE"):
        client = ProviderTransport("focus", "fake", peer.origin)
        client.send("FC-CIE", CIENCIA)
        # When the adapter tries to send it again.
        with pytest.raises(EnvioNaoAutorizado):
            client.send("FC-CIE", CIENCIA)
        # Then the provider saw a single request.
        assert len(peer.requests) == 1


def test_broker_tokens_need_a_serpro_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an open Focus dispatch only.
    with FakeSerpro((Reply(body=TOKENS),)) as peer:
        monkeypatch.setattr(token_broker, "BROKER_URL", peer.origin + "/token")
        with (
            wire_window("focus", "fake", "FC-NFE"),
            pytest.raises(EnvioNaoAutorizado),
        ):
            # When SERPRO tokens are requested.
            TokenBrokerClient("synthetic-hmac-secret").tokens("ABCDEF12345678")
        # Then the broker is not contacted.
        assert peer.requests == []


def test_only_the_dispatch_module_opens_the_provider_wire() -> None:
    # Given every application module.
    apps = Path(__file__).parents[2] / "apps"
    # When they are searched for the wire window and the issued-dispatch table.
    users = sorted(
        str(path.relative_to(apps))
        for path in apps.rglob("*.py")
        if re.search(r"\b(?:_wire_window|_WIRE|_ISSUED)\b", path.read_text())
    )
    # Then only call_dispatch, which grants it inside transmit(), touches them.
    assert users == ["integrations/call_dispatch.py"]

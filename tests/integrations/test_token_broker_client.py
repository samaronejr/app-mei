"""Real loopback exchanges verify per-call HMAC and absence of a worker cache."""

import hashlib
import hmac
import json

import pytest

from apps.integrations import token_broker
from apps.integrations.token_broker import TokenBrokerClient
from apps.integrations.transport_types import TransportFailure
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro

TOKENS = (
    b'{"access_token":"synthetic-access","jwt_token":"synthetic-jwt","expires_in":2008}'
)


def test_broker_client_signs_exact_body_and_does_not_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with FakeSerpro((Reply(body=TOKENS), Reply(body=TOKENS))) as peer:
        monkeypatch.setattr(token_broker, "BROKER_URL", peer.origin + "/token")
        client = TokenBrokerClient("synthetic-hmac-secret")
        first = client.tokens("ABCDEF12345678")
        second = client.tokens("ABCDEF12345678", refresh=True)
        assert first == second
        assert len(peer.requests) == 2
        for request in peer.requests:
            assert (request.method, request.path) == ("POST", "/token")
            signature = hmac.new(
                b"synthetic-hmac-secret", request.body, hashlib.sha256
            ).hexdigest()
            assert dict(request.headers)["X-Broker-Token-HMAC"] == signature
        assert json.loads(peer.requests[1].body)["refresh"] is True
        assert "synthetic-access" not in repr(first)
        assert "synthetic-jwt" not in repr(first)
        assert "synthetic-hmac-secret" not in repr(client)


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"[]",
        b"invalid",
        b'{"access_token":"x","jwt_token":"y","expires_in":true}',
        b'{"access_token":"x\\r\\nInjected","jwt_token":"y","expires_in":10}',
    ],
)
def test_broker_reply_is_strictly_validated(
    body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeSerpro((Reply(body=body),)) as peer:
        monkeypatch.setattr(token_broker, "BROKER_URL", peer.origin + "/token")
        with pytest.raises(TransportFailure):
            TokenBrokerClient("synthetic-secret").tokens("00000000000000")
        assert len(peer.requests) == 1


@pytest.mark.parametrize("status", [302, 401, 500])
def test_broker_failure_is_not_retried(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeSerpro((Reply(status=status, body=TOKENS),)) as peer:
        monkeypatch.setattr(token_broker, "BROKER_URL", peer.origin + "/token")
        with pytest.raises(TransportFailure):
            TokenBrokerClient("synthetic-secret").tokens("00000000000000")
        assert len(peer.requests) == 1

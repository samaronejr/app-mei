"""The isolated broker has no Django, persisted token cache, or key-bearing caller."""

import hashlib
import hmac
import logging
from pathlib import Path

import pytest

from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_broker_support import (
    ACCESS,
    HMAC_SECRET,
    JWT,
    KEY_PASSPHRASE,
    PROCURADOR,
    RECOVERED_ACCESS,
    REFRESHED_ACCESS,
    config,
    mutual_tls,
    oauth_reply,
    post,
    running_broker,
    signature,
    token_body,
)
from tests.integrations.serpro_support import FIRM, broker_module


@pytest.mark.parametrize("supplied", [None, "", "wrong-signature", "0" * 64])
def test_hmac_refusal_sends_nothing_to_sapi(
    tmp_path: Path, supplied: str | None
) -> None:
    # Given a broker whose upstream would issue a token over mutual TLS.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    with FakeSerpro((Reply(body=oauth_reply()),), tls=tls) as upstream:
        broker = module.TokenBroker(
            module.BrokerConfig(**config(tmp_path, upstream.origin))
        )
        with running_broker(module, broker) as origin:
            # When the internal caller omits or forges the exact-body HMAC.
            status, _ = post(origin, token_body(), supplied)
        # Then even a syntactically valid signature from another secret sends nothing.
        assert status == 401
        assert upstream.requests == []


def test_mutual_tls_issues_tokens_and_keeps_them_only_until_ttl_margin(
    tmp_path: Path,
) -> None:
    # Given an encrypted client key, self-signed CA and an injected clock.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    now = [1000.0]
    with FakeSerpro(
        (Reply(body=oauth_reply()), Reply(body=oauth_reply(REFRESHED_ACCESS))),
        tls=tls,
    ) as upstream:
        broker = module.TokenBroker(
            module.BrokerConfig(**config(tmp_path, upstream.origin)),
            now=lambda: now[0],
        )
        body = token_body()
        with running_broker(module, broker) as origin:
            # When requests straddle expires_at minus the required 120-second margin.
            first = post(origin, body, signature(body))
            now[0] = 2887
            cached = post(origin, body, signature(body))
            now[0] = 2888
            refreshed = post(origin, body, signature(body))
        # Then the cache hits before the margin and refreshes exactly at it.
        assert first[0] == cached[0] == refreshed[0] == 200
        assert isinstance(first[1], dict)
        assert isinstance(cached[1], dict)
        assert isinstance(refreshed[1], dict)
        for name in ("access_token", "jwt_token", "expires_at"):
            assert first[1][name] == cached[1][name]
        assert first[1]["access_token"] == ACCESS
        assert first[1]["jwt_token"] == JWT
        assert first[1]["expires_at"] == 3008
        assert refreshed[1]["access_token"] == REFRESHED_ACCESS
        assert len(upstream.requests) == 2
        for request in upstream.requests:
            assert (request.method, request.path) == ("POST", "/authenticate")
            headers = {name.lower(): value for name, value in request.headers}
            assert headers["role-type"] == "TERCEIROS"
            assert request.body == b"grant_type=client_credentials"


def test_explicit_refresh_drops_stale_tokens_even_if_authentication_fails(
    tmp_path: Path,
) -> None:
    # Given a cached OAuth token followed by an unavailable authentication service.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    with FakeSerpro(
        (
            Reply(body=oauth_reply()),
            Reply(status=503, body=b"{}"),
            Reply(body=oauth_reply(RECOVERED_ACCESS)),
        ),
        tls=tls,
    ) as upstream:
        broker = module.TokenBroker(
            module.BrokerConfig(**config(tmp_path, upstream.origin))
        )
        ordinary, refresh = token_body(), token_body(refresh=True)
        with running_broker(module, broker) as origin:
            first = post(origin, ordinary, signature(ordinary))
            # When the next attempt explicitly invalidates the previous token.
            refused = post(origin, refresh, signature(refresh))
            after = post(origin, ordinary, signature(ordinary))
        # Then failed refresh cannot resurrect the cached credential.
        assert first[0] == after[0] == 200
        assert refused[0] == 503
        assert isinstance(after[1], dict)
        assert after[1]["access_token"] == RECOVERED_ACCESS
        assert len(upstream.requests) == 3


def test_fake_environment_refuses_even_authenticated_token_requests(
    tmp_path: Path,
) -> None:
    # Given a correctly authenticated broker request in the shipped default environment.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    with FakeSerpro((Reply(body=oauth_reply()),), tls=tls) as upstream:
        values = config(tmp_path, upstream.origin)
        values["environment"] = "fake"
        broker = module.TokenBroker(module.BrokerConfig(**values))
        body = token_body()
        with running_broker(module, broker) as origin:
            # When a caller asks it to mint credentials.
            status, value = post(origin, body, signature(body))
        # Then fake mode cannot silently acquire real credentials.
        assert (status, value) == (503, {"code": "ambiente_recusado"})
        assert upstream.requests == []


@pytest.mark.parametrize("environment", ["fake", "trial"])
def test_non_production_broker_refuses_before_any_upstream_request(
    tmp_path: Path, environment: str
) -> None:
    # Given the token store itself, behind the HTTP endpoint's own refusal.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    with FakeSerpro((Reply(body=oauth_reply()),), tls=tls) as upstream:
        values = config(tmp_path, upstream.origin)
        values["environment"] = environment
        broker = module.TokenBroker(module.BrokerConfig(**values))
        # When any caller reaches it outside production, then it mints nothing.
        with pytest.raises(module.BrokerUnavailableError):
            broker.tokens(FIRM)
        assert upstream.requests == []


def test_broker_without_a_secret_refuses_even_an_empty_key_signature(
    tmp_path: Path,
) -> None:
    # Given a broker deployed without SERPRO_BROKER_SECRET.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    with FakeSerpro((Reply(body=oauth_reply()),), tls=tls) as upstream:
        values = config(tmp_path, upstream.origin)
        values["secret"] = ""
        broker = module.TokenBroker(module.BrokerConfig(**values))
        body = token_body()
        empty_key = hmac.new(b"", body, hashlib.sha256).hexdigest()
        with running_broker(module, broker) as origin:
            # When a caller signs with the same empty key.
            status, _ = post(origin, body, empty_key)
        # Then an unconfigured secret authenticates nobody.
        assert status == 401
        assert upstream.requests == []


def test_malformed_authentication_reply_is_unavailable_not_a_caller_error(
    tmp_path: Path,
) -> None:
    # Given SAPI answers 200 with a body that is not JSON.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    with FakeSerpro((Reply(body=b"not-json"),), tls=tls) as upstream:
        broker = module.TokenBroker(
            module.BrokerConfig(**config(tmp_path, upstream.origin))
        )
        body = token_body()
        with running_broker(module, broker) as origin:
            # When a correctly signed caller asks for tokens.
            status, value = post(origin, body, signature(body))
    # Then it is an upstream outage, never a caller error or a leaked body.
    assert (status, value) == (503, {"code": "autenticacao_indisponivel"})


def test_procurador_token_is_firm_scoped_ephemeral_and_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given a firm token held only in one broker's process memory.
    module = broker_module()
    tls, _ = mutual_tls(tmp_path)
    caplog.set_level(logging.DEBUG)
    with FakeSerpro((Reply(body=oauth_reply()),), tls=tls) as upstream:
        configuration = module.BrokerConfig(**config(tmp_path, upstream.origin))
        broker = module.TokenBroker(configuration, now=lambda: 1000.0)
        broker.store_procurador(FIRM, PROCURADOR, 2000.0)
        body = token_body()
        with running_broker(module, broker) as origin:
            status, value = post(origin, body, signature(body))
        # When another process starts with the same configuration, it has no firm token.
        restarted = module.TokenBroker(configuration, now=lambda: 1000.0)
        # Then tokens are absent from process logs, errors and diagnostic reprs.
        assert status == 200
        assert isinstance(value, dict)
        assert value["autenticar_procurador_token"] == PROCURADOR
        assert restarted.procurador_for(FIRM) == ""
        assert broker.procurador_for("11444777000161") == ""
        captured = capsys.readouterr()
        text = (
            caplog.text
            + captured.out
            + captured.err
            + repr(configuration)
            + repr(broker)
        )
        for canary in (HMAC_SECRET, ACCESS, JWT, PROCURADOR, KEY_PASSPHRASE):
            assert canary not in text


def test_expired_procurador_token_is_not_returned(tmp_path: Path) -> None:
    # Given a cached termo token whose expiry the clock reaches.
    module = broker_module()
    values = config(tmp_path, "https://127.0.0.1:1")
    now = [1000.0]
    broker = module.TokenBroker(module.BrokerConfig(**values), now=lambda: now[0])
    broker.store_procurador(FIRM, PROCURADOR, 1001.0)
    now[0] = 1001.0
    # When the firm asks for its token, then expired authority is not returned.
    assert broker.procurador_for(FIRM) == ""

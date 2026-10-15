"""Candidate adapter exercises a real loopback HTTP exchange, without a vendor call."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from django.db import connection
from pytest_django.fixtures import SettingsWrapper

from apps.automation.classification import messages_for, schema
from apps.automation.provider import OpenAIClient
from apps.automation.types import Budget, ProviderError
from tests.portal.request_fixtures import PortalSetup


@contextmanager
def wire(raw_body: bytes | None = None) -> Iterator[tuple[HTTPServer, list[bytes]]]:
    bodies: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            content = json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "{}"},
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                }
            ).encode()
            if raw_body is not None:
                content = raw_body
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        # Exactly one request: handle_request exits on receipt, no sleep/poll loop.
        server.timeout = 10
        thread = Thread(target=server.handle_request)
        thread.start()
        try:
            yield server, bodies
        finally:
            thread.join(timeout=12)
            assert not thread.is_alive()


@pytest.mark.django_db(transaction=True)
def test_wire_payload_has_schema_no_tools_and_no_transaction(
    setup: PortalSetup, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.AI_PROVIDER = "openai"
    settings.AI_REAL_DATA_APPROVED = True
    settings.AI_MODEL = "test-fixture"
    settings.AI_API_KEY = "fixture"
    settings.AI_INPUT_BRL_PER_MILLION = "1"
    settings.AI_OUTPUT_BRL_PER_MILLION = "1"
    with wire() as (server, bodies):

        def transport(host: str, timeout: int) -> HTTPConnection:
            assert host == "api.openai.com"
            assert not connection.in_atomic_block
            return HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)

        monkeypatch.setattr("apps.automation.provider.HTTPSConnection", transport)
        reply = OpenAIClient(setup.graph.tenant.pk).complete(
            schema(), messages_for("11222333000181", "synthetic"), Budget()
        )
    assert reply.output == "{}"
    payload = json.loads(bodies[0])
    assert payload["store"] is False
    assert payload["max_completion_tokens"] == 512
    assert payload["response_format"]["json_schema"]["schema"] == schema()
    assert not {"tools", "functions", "web_search_options"} & payload.keys()


@pytest.mark.django_db(transaction=True)
def test_real_adapter_refuses_without_owner_gate(
    setup: PortalSetup, settings: SettingsWrapper
) -> None:
    settings.AI_PROVIDER = "openai"
    settings.AI_REAL_DATA_APPROVED = False
    with pytest.raises(ProviderError, match="gate_closed"):
        OpenAIClient(setup.graph.tenant.pk).complete(
            schema(), messages_for("11222333000181", "synthetic"), Budget()
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"choices":[]}',
        b'{"choices":[{"finish_reason":"stop","message":null}],"usage":{}}',
    ],
)
def test_malformed_wire_response_becomes_a_sanitized_provider_error(
    setup: PortalSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    settings.AI_PROVIDER = "openai"
    settings.AI_REAL_DATA_APPROVED = True
    settings.AI_MODEL = "test-fixture"
    settings.AI_API_KEY = "fixture"
    settings.AI_INPUT_BRL_PER_MILLION = "1"
    settings.AI_OUTPUT_BRL_PER_MILLION = "1"
    with wire(body) as (server, bodies):

        def transport(host: str, timeout: int) -> HTTPConnection:
            assert host == "api.openai.com"
            return HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)

        monkeypatch.setattr("apps.automation.provider.HTTPSConnection", transport)
        with pytest.raises(ProviderError, match="provider_response_invalid"):
            OpenAIClient(setup.graph.tenant.pk).complete(
                schema(), messages_for("11222333000181", "synthetic"), Budget()
            )
    assert len(bodies) == 1

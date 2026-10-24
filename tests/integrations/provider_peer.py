"""An ephemeral HTTP peer that can reset after receiving a synthetic request."""

from __future__ import annotations

import hashlib
import http.client
import socket
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Self

from django.db import connection

from apps.integrations.call_contracts import CallResult
from apps.integrations.call_models import CallState

if TYPE_CHECKING:
    from apps.integrations.call_protocol import Dispatch
    from apps.integrations.firm_calls import FirmDispatch


class ProviderPeer:
    """Mutable wire observations, scoped to one test and one ephemeral port."""

    def __init__(self, *, reset: bool = False, status: int = 200) -> None:
        self.requests: list[str] = []
        self.received = threading.Event()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                peer.requests.append(self.headers.get("X-Request-Tag", ""))
                peer.received.set()
                if reset:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = b'{"synthetic":"ok"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: str) -> None:  # noqa: A002
                """Keep response bytes and request headers out of test evidence."""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="provider-peer", daemon=True
        )

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()

    def send(self, dispatch: Dispatch | FirmDispatch) -> CallResult:
        """The real wire receives exactly one request, outside a DB transaction."""
        assert not connection.in_atomic_block
        with closing(
            http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        ) as client:
            client.request(
                "POST",
                "/synthetic",
                body=b"{}",
                headers={"X-Request-Tag": dispatch.claim.owner.hex},
            )
            response = client.getresponse()
            body = response.read()
        return CallResult(
            CallState.RETRY if response.status == 503 else CallState.SUCCESS,
            response.status,
            "temporary" if response.status == 503 else "ok",
            "sim",
            hashlib.sha256(body).hexdigest(),
        )

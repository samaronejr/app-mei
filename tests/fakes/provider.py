"""Scripted loopback HTTP peers with event-held replies, never timing sleeps."""

from __future__ import annotations

import socket
import ssl
import threading
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from types import TracebackType


@dataclass(frozen=True, slots=True)
class Reply:
    status: int = 200
    body: bytes = b'{"status":200}'
    headers: tuple[tuple[str, str], ...] = ()
    held: bool = False
    reset: bool = False
    declared_length: bool = True
    chunks: tuple[bytes, ...] = ()


@dataclass(frozen=True, slots=True)
class Received:
    method: str
    path: str
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)


class _Server(HTTPServer):
    peer: ScriptedProvider


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: str) -> None:  # noqa: A002
        """No bodies, request URLs or headers in process logs."""

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()

    def _respond(self) -> None:
        peer = self.server.peer
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        peer.requests.append(
            Received(self.command, self.path, body, tuple(self.headers.items()))
        )
        peer.received.set()
        reply = peer.replies.popleft() if peer.replies else Reply(status=500)
        if reply.reset:
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if reply.held:
            peer.release.wait(timeout=5)
        try:
            self.send_response(reply.status)
            for key, value in reply.headers:
                self.send_header(key, value)
            if reply.declared_length:
                self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            for part in reply.chunks or (reply.body,):
                self.wfile.write(part)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            # Expected when the client rejects headers/size or times out.
            return


class ScriptedProvider:
    """One script queue and request log per ephemeral server, owned by the test."""

    def __init__(
        self, replies: tuple[Reply, ...], *, tls: ssl.SSLContext | None = None
    ) -> None:
        self.replies = deque(replies)
        self.requests: list[Received] = []
        self.received = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.peer = self
        if tls is not None:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self.origin = f"{'https' if tls else 'http'}://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.closed.is_set():
            self.server.handle_request()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release.set()
        self.closed.set()
        self.server.socket.shutdown(socket.SHUT_RDWR)
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()

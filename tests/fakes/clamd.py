"""A deterministic fake clamd on 127.0.0.1 (AXRM-005): PING, VERSION and INSTREAM.

It speaks the same null-terminated `z` protocol `apps.cycles.scanning.ClamdScanner`
uses, one command per connection, and records every command it received:

- `zPING` answers `ping_reply` (`PONG` by default);
- `zVERSION` answers `version`;
- `zINSTREAM` reads the length-prefixed frames to the zero frame and answers
  `stream: Eicar-Signature FOUND` when the EICAR string is anywhere in the body
  (the real clamd finds it inside a PDF stream object), else `stream: OK`.
  `detect=False` makes it answer OK to everything: a scanner that misses.

`silent=True` accepts, reads the command and never answers, which is the adapter's
timeout path; the held connection is released by `close()`, so nothing waits on a
wall clock here.
"""

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from apps.cycles.scanning import EICAR

VERSION: Final = b"ClamAV 1.4.6/28150/Sun Oct 11 06:26:01 2026"
SIGNATURE_VERSION: Final = "28150/Sun Oct 11 06:26:01 2026"
ENGINE: Final = "ClamAV 1.4.6"


class FakeClamd:
    """One listening socket served by one thread, a connection at a time."""

    def __init__(
        self,
        *,
        silent: bool = False,
        detect: bool = True,
        ping_reply: bytes = b"PONG",
        version: bytes = VERSION,
    ) -> None:
        self.silent = silent
        self.detect = detect
        self.ping_reply = ping_reply
        self.version = version
        self.commands: list[bytes] = []
        self.scanned: list[bytes] = []
        self._release = threading.Event()
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _read(conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            part = conn.recv(size - len(data))
            if not part:
                break
            data += part
        return data

    def _instream(self, conn: socket.socket) -> bytes:
        body = b""
        while True:
            size = int.from_bytes(self._read(conn, 4), "big")
            if size == 0:
                break
            body += self._read(conn, size)
        self.scanned.append(body)
        if self.detect and EICAR in body:
            return b"stream: Eicar-Signature FOUND"
        return b"stream: OK"

    def _answer(self, conn: socket.socket, command: bytes) -> bytes:
        if command == b"zPING\0":
            return self.ping_reply
        if command == b"zVERSION\0":
            return self.version
        if command == b"zINSTREAM\0":
            return self._instream(conn)
        return b"UNKNOWN COMMAND"

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                command = b""
                while not command.endswith(b"\0"):
                    part = conn.recv(1)
                    if not part:
                        break
                    command += part
                self.commands.append(command)
                if self.silent:
                    # Held open until close(): the client's own timeout must fire.
                    self._release.wait()
                    continue
                conn.sendall(self._answer(conn, command) + b"\0")

    def close(self) -> None:
        """Release a held connection, stop listening and join the thread."""
        self._release.set()
        self._server.close()
        self._thread.join(timeout=5)


@contextmanager
def fake_clamd(
    *,
    silent: bool = False,
    detect: bool = True,
    ping_reply: bytes = b"PONG",
) -> Iterator[FakeClamd]:
    """Run a fake clamd for the block, then close it."""
    server = FakeClamd(silent=silent, detect=detect, ping_reply=ping_reply)
    try:
        yield server
    finally:
        server.close()

"""Ephemeral test-only certificate and DNS mapping; never trust real endpoints."""

import socket
import ssl
import subprocess
from pathlib import Path

import pytest

HOST = "focusnfe.s3.sa-east-1.amazonaws.com"


def tls_context(tmp_path: Path) -> tuple[ssl.SSLContext, str]:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    # Generated test key exists only in pytest's per-test temporary directory.
    subprocess.run(  # noqa: S603
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=synthetic-provider",
            "-addext",
            f"subjectAltName=IP:127.0.0.1,DNS:{HOST}",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context, str(cert)


def route_storage_host(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    original = socket.getaddrinfo

    def resolve(
        host: str | bytes | None,
        service: str | bytes | int | None,
        family: int = 0,
        *options: int,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
        ]
    ]:
        assert host in {HOST, "127.0.0.1"}
        return original(
            "127.0.0.1", port if host == HOST else service, family, *options
        )

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

"""In-process HTTP broker and mutual-TLS peer helpers, with event-owned teardown."""

import hashlib
import hmac
import json
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Protocol, TypedDict
from urllib.parse import urlsplit

from apps.integrations.transport_types import JSONValue
from tests.integrations.serpro_support import FIRM
from tests.integrations.transport_tls import tls_context

HMAC_SECRET = hashlib.sha256(b"synthetic-broker-hmac-013").hexdigest()
ACCESS = "synthetic-broker-access-013"
JWT = "synthetic-broker-jwt-013"
PROCURADOR = "synthetic-procurador-013"
KEY_PASSPHRASE = hashlib.sha256(b"synthetic-encrypted-key-pass-013").hexdigest()
REFRESHED_ACCESS = "synthetic-refreshed"
RECOVERED_ACCESS = "synthetic-after-failure"


class BrokerSettings(TypedDict):
    environment: str
    secret: str
    consumer_key: str
    consumer_secret: str
    certfile: str
    keyfile: str
    password_file: str
    authentication_url: str
    ca_file: str


class BrokerInstance(Protocol):
    def procurador_for(self, firm_cnpj: str) -> str: ...


def oauth_reply(access: str = ACCESS) -> bytes:
    return json.dumps(
        {"access_token": access, "jwt_token": JWT, "expires_in": 2008}
    ).encode()


def mutual_tls(tmp_path: Path) -> tuple[ssl.SSLContext, str]:
    context, ca = tls_context(tmp_path)
    context.load_verify_locations(cafile=ca)
    context.verify_mode = ssl.CERT_REQUIRED
    # The synthetic client key is encrypted, exercising load_cert_chain(password).
    subprocess.run(  # noqa: S603 - fixed openssl command, test-owned paths
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(tmp_path / "key.pem"),
            "-aes-256-cbc",
            "-passout",
            "stdin",
            "-out",
            str(tmp_path / "encrypted-key.pem"),
        ],
        input=(KEY_PASSPHRASE + "\n").encode(),
        capture_output=True,
        check=True,
    )
    (tmp_path / "key-pass").write_text(KEY_PASSPHRASE)
    return context, ca


def config(tmp_path: Path, origin: str) -> BrokerSettings:
    return {
        "environment": "producao",
        "secret": HMAC_SECRET,
        "consumer_key": "synthetic-consumer-013",
        "consumer_secret": "synthetic-consumer-secret-013",
        "certfile": str(tmp_path / "cert.pem"),
        "keyfile": str(tmp_path / "encrypted-key.pem"),
        "password_file": str(tmp_path / "key-pass"),
        "authentication_url": origin + "/authenticate",
        "ca_file": str(tmp_path / "cert.pem"),
    }


@contextmanager
def running_broker(module: ModuleType, broker: BrokerInstance) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), module.make_handler(broker))
    closed = threading.Event()

    def serve() -> None:
        while not closed.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        closed.set()
        server.socket.shutdown(socket.SHUT_RDWR)
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def token_body(*, refresh: bool = False) -> bytes:
    return json.dumps(
        {"firm_cnpj": FIRM, "refresh": refresh},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def signature(body: bytes) -> str:
    return hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()


def post(origin: str, body: bytes, signed: str | None) -> tuple[int, JSONValue]:
    endpoint = urlsplit(origin)
    host = endpoint.hostname
    assert host is not None
    headers = {"Content-Type": "application/json"}
    if signed is not None:
        headers["X-Broker-Token-HMAC"] = signed
    with closing(HTTPConnection(host, endpoint.port, timeout=5)) as client:
        client.request("POST", "/token", body=body, headers=headers)
        response = client.getresponse()
        value: JSONValue = json.loads(response.read())
        return response.status, value

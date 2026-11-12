"""Focus scripted server, including one-hop PDF redirects and receipt streams."""

from __future__ import annotations

import json
import socket
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Self
from urllib.parse import parse_qs, urlsplit

from tests.fakes.provider import Received, Reply, ScriptedProvider

if TYPE_CHECKING:
    from types import TracebackType


class FakeFocus(ScriptedProvider):
    """Use the same wire script semantics as SERPRO, on an independent port."""


def synthetic_company(
    company_id: int, cnpj: str, **fields: object
) -> dict[str, object]:
    """One synthetic `/v2/empresas/{id}` body, shaped like Focus's documented reply.

    It carries the provider's own token fields on purpose: Axorim must never copy
    them anywhere, so every test reads them back as present on the wire only.
    """
    company: dict[str, object] = {
        "id": company_id,
        "nome": "Empresa sintetica",
        "cnpj": cnpj,
        "habilita_nfe": False,
        "habilita_nfce": False,
        "habilita_nfse": False,
        "habilita_nfsen_producao": False,
        "habilita_nfsen_homologacao": False,
        "habilita_cte": False,
        "habilita_mdfe": False,
        "habilita_manifestacao": False,
        "habilita_manifestacao_homologacao": False,
        "habilita_nfsen_recebidas_producao": False,
        "habilita_nfsen_recebidas_homologacao": False,
        "habilita_filtro_mde_destinatario_empresa": False,
        "certificado_especifico": False,
        "certificado_cnpj": cnpj,
        "certificado_valido_de": "2026-01-10T09:00:00-03:00",
        "certificado_valido_ate": "2027-01-10T09:00:00-03:00",
        "data_inicio_recebimento_nfe": None,
        "data_inicio_recebimento_nfsen": None,
        "senha_responsavel_preenchida": True,
        "token_producao": "SYNTHETICPRODUCTIONTOKEN0000000000",
        "token_homologacao": "SYNTHETICHOMOLOGATIONTOKEN00000000",
    }
    company.update(fields)
    return company


class _CompanyServer(HTTPServer):
    peer: FakeFocusCompany


class _CompanyHandler(BaseHTTPRequestHandler):
    server: _CompanyServer

    def log_message(self, format: str, *args: str) -> None:  # noqa: A002
        """No bodies, request URLs or headers in process logs."""

    def do_GET(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def _respond(self) -> None:
        peer = self.server.peer
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with peer.lock:
            peer.requests.append(
                Received(self.command, self.path, body, tuple(self.headers.items()))
            )
            scripted = peer.script.popleft() if peer.script else None
            if scripted is None:
                status, payload = peer.answer(self.command, self.path, body)
                scripted = Reply(status=status, body=payload)
        if scripted.reset:
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        try:
            self.send_response(scripted.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(scripted.body)))
            self.end_headers()
            self.wfile.write(scripted.body)
        except (BrokenPipeError, ConnectionResetError):
            # Expected when the client refuses an oversized reply mid-body.
            return


class FakeFocusCompany:
    """A stateful synthetic Focus company: GET reads it, PUT merges into it.

    `PUT ...?dry_run=1` validates without changing anything, as Focus documents.
    `script` holds replies served before the state machine (a `None` entry means
    "answer from state"); `apply_puts=False` acknowledges PUTs without applying them.
    """

    def __init__(self, company: dict[str, object]) -> None:
        self.company = company
        self.requests: list[Received] = []
        self.script: deque[Reply | None] = deque()
        self.apply_puts = True
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.server = _CompanyServer(("127.0.0.1", 0), _CompanyHandler)
        self.server.peer = self
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def answer(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        parsed = urlsplit(path)
        expected = f"/v2/empresas/{self.company['id']}"
        if parsed.path != expected:
            return 404, b'{"codigo":"nao_encontrado"}'
        if method == "PUT":
            changes = json.loads(body)
            dry_run = parse_qs(parsed.query).get("dry_run") == ["1"]
            if self.apply_puts and not dry_run:
                self.company.update(changes)
            return 200, json.dumps({**self.company, **changes}).encode()
        if method == "GET":
            return 200, json.dumps(self.company).encode()
        return 405, b"{}"

    def bodies(self, method: str = "PUT") -> list[dict[str, object]]:
        return [
            json.loads(item.body) for item in self.requests if item.method == method
        ]

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
        self.closed.set()
        self.server.socket.shutdown(socket.SHUT_RDWR)
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()

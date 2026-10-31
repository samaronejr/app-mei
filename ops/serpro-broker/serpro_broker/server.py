"""Authenticate bounded internal requests without logging their credentials."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, ClassVar

from .cache import LIMIT, BrokerUnavailableError, TokenBroker

if TYPE_CHECKING:
    from .cache import JSON, TokenView


class InvalidRequestError(Exception):
    """A symbolic malformed internal request."""


@dataclass(frozen=True, slots=True)
class TokenRequest:
    """A parsed OAuth lookup with an explicit refresh choice."""

    firm: str
    refresh: bool


@dataclass(frozen=True, slots=True)
class ProcuradorRequest:
    """An authenticated transient handoff for one firm."""

    firm: str
    token: str = field(repr=False)
    expiry: float


def _parse(path: str, body: bytes) -> TokenRequest | ProcuradorRequest:
    value: JSON = json.loads(body)
    if not isinstance(value, dict):
        raise InvalidRequestError
    firm = value.get("firm_cnpj")
    if not isinstance(firm, str) or re.fullmatch(r"[A-Z0-9]{12}[0-9]{2}", firm) is None:
        raise InvalidRequestError
    if path == "/token":
        refresh = value.get("refresh")
        if set(value) != {"firm_cnpj", "refresh"} or type(refresh) is not bool:
            raise InvalidRequestError
        return TokenRequest(firm, refresh)
    token, expiry = value.get("procurador"), value.get("expires_at")
    if (
        set(value) != {"firm_cnpj", "procurador", "expires_at"}
        or not isinstance(token, str)
        or not isinstance(expiry, int | float)
        or isinstance(expiry, bool)
    ):
        raise InvalidRequestError
    return ProcuradorRequest(firm, token, float(expiry))


class _Handler(BaseHTTPRequestHandler):
    broker: ClassVar[TokenBroker]

    def log_message(self, format: str, *args: str) -> None:  # noqa: A002
        """Never log request bytes, URLs, headers or secrets."""

    def reply(self, status: HTTPStatus, value: TokenView | dict[str, str]) -> None:
        """Return a non-cacheable internal reply."""
        data = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        """Expose liveness, not credentials, without authentication."""
        status = HTTPStatus.OK if self.path == "/healthz" else HTTPStatus.NOT_FOUND
        self.reply(status, {"status": "ok"})

    def _authenticate_body(self) -> bytes | None:
        self.connection.settimeout(5)
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= LIMIT:
            self.reply(HTTPStatus.BAD_REQUEST, {"code": "pedido_invalido"})
            return None
        body = self.rfile.read(length)
        expected = hmac.new(
            self.broker.config.secret.encode(), body, hashlib.sha256
        ).hexdigest()
        supplied = self.headers.get("X-Broker-Token-HMAC", "")
        if (
            not self.broker.config.secret
            or len(body) != length
            or re.fullmatch(r"[a-f0-9]{64}", supplied) is None
            or not hmac.compare_digest(expected, supplied)
        ):
            self.reply(HTTPStatus.UNAUTHORIZED, {"code": "hmac_invalido"})
            return None
        return body

    def do_POST(self) -> None:
        """Authenticate the exact bounded body before any credential operation."""
        try:
            body = self._authenticate_body()
            if body is None:
                return
            if self.broker.config.environment != "producao":
                self.reply(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"code": "ambiente_recusado"}
                )
                return
            if self.path not in {"/token", "/procurador"}:
                self.reply(HTTPStatus.NOT_FOUND, {"code": "rota_invalida"})
                return
            request = _parse(self.path, body)
            match request:
                case TokenRequest(firm, refresh):
                    self.reply(HTTPStatus.OK, self.broker.tokens(firm, refresh=refresh))
                case ProcuradorRequest(firm, token, expiry):
                    self.broker.store_procurador(firm, token, expiry)
                    self.reply(HTTPStatus.OK, {"status": "ok"})
        except (ValueError, KeyError, RecursionError, InvalidRequestError):
            self.reply(HTTPStatus.BAD_REQUEST, {"code": "pedido_invalido"})
        except (OSError, HTTPException, BrokerUnavailableError):
            self.reply(
                HTTPStatus.SERVICE_UNAVAILABLE, {"code": "autenticacao_indisponivel"}
            )


def make_handler(broker: TokenBroker) -> type[BaseHTTPRequestHandler]:
    """Bind one process-local token store to the internal HTTP handler."""

    class Handler(_Handler):
        pass

    Handler.broker = broker
    return Handler

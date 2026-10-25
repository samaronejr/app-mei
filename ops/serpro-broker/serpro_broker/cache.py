"""Keep OAuth and firm credentials only in the broker process's memory."""

from __future__ import annotations

import base64
import json
import math
import re
import ssl
import time
from contextlib import closing
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPSConnection
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypedDict
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

AUTH_URL: Final = "https://autenticacao.sapi.serpro.gov.br/authenticate"
LIMIT: Final = 65536
MARGIN: Final = 120
TOKEN: Final = re.compile(r"[A-Za-z0-9_.~+/-]{1,16384}={0,2}")
type JSON = str | int | float | bool | list[JSON] | dict[str, JSON] | None


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    """Deployment values never participate in exception or diagnostic text."""

    environment: str = "fake"
    secret: str = field(default="", repr=False)
    consumer_key: str = field(default="", repr=False)
    consumer_secret: str = field(default="", repr=False)
    certfile: str = "/run/secrets/serpro_contratante_cert.pem"
    keyfile: str = "/run/secrets/serpro_contratante_key.pem"
    password_file: str = field(
        default="/run/secrets/serpro_contratante_key_pass", repr=False
    )
    authentication_url: str = AUTH_URL
    ca_file: str = ""


@dataclass(frozen=True, slots=True)
class _Tokens:
    access: str = field(repr=False)
    jwt: str = field(repr=False)
    expires_at: float


class TokenView(TypedDict):
    """The internal response is not cacheable or persistable by the caller."""

    access_token: str
    jwt_token: str
    expires_at: float
    expires_in: int
    autenticar_procurador_token: str


class BrokerUnavailableError(Exception):
    """A symbolic failure; no upstream value or TLS exception is exposed."""


class TokenBroker:
    """Own mutable memory in the single-threaded internal HTTP server."""

    def __init__(
        self, config: BrokerConfig, *, now: Callable[[], float] = time.time
    ) -> None:
        """Start with empty OAuth and firm-token stores, including after restart."""
        self.config = config
        self.now = now
        self._oauth: _Tokens | None = None
        self._firms: dict[str, tuple[str, float]] = {}

    def _authenticate(self) -> _Tokens:
        config = self.config
        endpoint = urlsplit(config.authentication_url)
        if (
            endpoint.scheme != "https"
            or endpoint.hostname is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path != "/authenticate"
            or endpoint.query
            or endpoint.fragment
        ):
            raise BrokerUnavailableError
        context = ssl.create_default_context(cafile=config.ca_file or None)
        context.load_cert_chain(
            config.certfile,
            config.keyfile,
            password=Path(config.password_file).read_text().rstrip("\r\n"),
        )
        credential = base64.b64encode(
            f"{config.consumer_key}:{config.consumer_secret}".encode()
        ).decode()
        with closing(
            HTTPSConnection(
                endpoint.hostname, endpoint.port, timeout=10, context=context
            )
        ) as client:
            client.request(
                "POST",
                "/authenticate",
                body=b"grant_type=client_credentials",
                headers={
                    "Authorization": f"Basic {credential}",
                    "role-type": "TERCEIROS",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept-Encoding": "identity",
                },
            )
            response = client.getresponse()
            data = response.read(LIMIT + 1)
            if response.status != HTTPStatus.OK or len(data) > LIMIT:
                raise BrokerUnavailableError
        try:
            value: JSON = json.loads(data)
        except (ValueError, RecursionError):
            raise BrokerUnavailableError from None
        if not isinstance(value, dict):
            raise BrokerUnavailableError
        access, jwt, expires = (
            value.get("access_token"),
            value.get("jwt_token"),
            value.get("expires_in"),
        )
        if (
            not isinstance(access, str)
            or TOKEN.fullmatch(access) is None
            or not isinstance(jwt, str)
            or TOKEN.fullmatch(jwt) is None
            or type(expires) is not int
            or expires <= 0
        ):
            raise BrokerUnavailableError
        return _Tokens(access, jwt, self.now() + expires)

    def tokens(self, firm: str, *, refresh: bool = False) -> TokenView:
        """Drop stale credentials before refreshing; never fall back after failure."""
        if self.config.environment != "producao":
            raise BrokerUnavailableError
        if refresh:
            self._oauth = None
        pair = self._oauth
        if pair is None or self.now() >= pair.expires_at - MARGIN:
            self._oauth = None
            pair = self._authenticate()
            self._oauth = pair
        return {
            "access_token": pair.access,
            "jwt_token": pair.jwt,
            "expires_at": pair.expires_at,
            "expires_in": max(1, int(pair.expires_at - self.now())),
            "autenticar_procurador_token": self.procurador_for(firm),
        }

    def store_procurador(self, firm: str, value: str, expires_at: float) -> None:
        """Accept an authenticated handoff only into this process's firm partition."""
        if (
            TOKEN.fullmatch(value) is None
            or not math.isfinite(expires_at)
            or expires_at <= self.now()
        ):
            raise BrokerUnavailableError
        self._firms[firm] = (value, expires_at)

    def procurador_for(self, firm: str) -> str:
        """Expire entries on access, with no disk, database or Redis dependency."""
        item = self._firms.get(firm)
        if item is None:
            return ""
        if self.now() >= item[1]:
            del self._firms[firm]
            return ""
        return item[0]

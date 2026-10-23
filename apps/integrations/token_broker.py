"""Worker-side token retrieval only; certificate custody belongs to the broker."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final

from requests import Request

from apps.core.tasks import assert_no_transaction
from apps.integrations.call_dispatch import require_dispatch
from apps.integrations.transport_types import TransportFailure
from apps.integrations.transport_wire import WireLimits, _exchange

BROKER_URL: Final = "http://serpro-broker:8080/token"
TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_.~+/-]{1,16384}={0,2}")


@dataclass(frozen=True, slots=True)
class BrokerTokens:
    """Per-call memory only; repr never includes token material."""

    access_token: str = field(repr=False)
    jwt_token: str = field(repr=False)
    expires_in: int
    procurador_token: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class TokenBrokerClient:
    """HMAC covers the exact POST bytes; no token cache, auth retry or certificate."""

    secret: str = field(repr=False)

    def tokens(self, firm_cnpj: str, *, refresh: bool = False) -> BrokerTokens:
        """Request a fresh broker view for this call, failing closed on bad replies."""
        assert_no_transaction()
        require_dispatch("serpro")
        if not self.secret or re.fullmatch(r"[A-Z0-9]{12}[0-9]{2}", firm_cnpj) is None:
            msg = "broker_configuracao_invalida"
            raise TransportFailure(msg)
        body = json.dumps(
            {"firm_cnpj": firm_cnpj, "refresh": refresh},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        response = _exchange(
            Request(
                "POST",
                BROKER_URL,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Broker-Token-HMAC": signature,
                },
            ),
            WireLimits(size=65536),
        )
        if isinstance(response, str) or response.status != HTTPStatus.OK:
            msg = "broker_recusado"
            raise TransportFailure(msg)
        value = response.json()
        if not isinstance(value, dict):
            msg = "broker_resposta_invalida"
            raise TransportFailure(msg)
        access = value.get("access_token")
        jwt = value.get("jwt_token")
        expires = value.get("expires_in")
        procurador = value.get("autenticar_procurador_token", "")
        if (
            not isinstance(access, str)
            or TOKEN_PATTERN.fullmatch(access) is None
            or not isinstance(jwt, str)
            or TOKEN_PATTERN.fullmatch(jwt) is None
            or type(expires) is not int
            or expires <= 0
            or not isinstance(procurador, str)
            or (procurador and TOKEN_PATTERN.fullmatch(procurador) is None)
        ):
            msg = "broker_resposta_invalida"
            raise TransportFailure(msg)
        return BrokerTokens(access, jwt, expires, procurador)

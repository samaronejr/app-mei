"""Transient termo tokens from escaped 200 JSON or body-less 304 headers."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Final

from apps.integrations.serpro.codec import parse_response
from apps.integrations.token_broker import TOKEN_PATTERN
from apps.integrations.transport_types import ProviderResponse, TransportFailure

MONTHS: Final = {
    "fev": "Feb",
    "abr": "Apr",
    "mai": "May",
    "ago": "Aug",
    "set": "Sep",
    "out": "Oct",
    "dez": "Dec",
}


@dataclass(frozen=True, slots=True)
class TermoToken:
    """The caller hands this to the broker, never to persistence or diagnostics."""

    procurador_token: str = field(repr=False)
    expires_at: float


def _cached(response: ProviderResponse) -> tuple[str, float]:
    headers = {name.lower(): value for name, value in response.headers}
    prefix, separator, value = headers.get("etag", "").strip('"').partition(":")
    if prefix != "autenticar_procurador_token" or not separator:
        raise ValueError
    expiry = headers["expires"]
    for portuguese, english in MONTHS.items():
        expiry = re.sub(rf"\b{portuguese}\b", english, expiry, flags=re.IGNORECASE)
    if "," in expiry:
        expiry = expiry.split(",", 1)[1].strip()
    parsed = parsedate_to_datetime(expiry)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return value, parsed.timestamp()


def _issued(response: ProviderResponse) -> tuple[str, float]:
    data = parse_response(response).data
    if not isinstance(data, dict):
        raise TypeError
    value, expiry = (
        data.get("autenticar_procurador_token"),
        data.get("data_hora_expiracao"),
    )
    if not isinstance(value, str) or not isinstance(expiry, str):
        raise TypeError
    parsed = datetime.fromisoformat(expiry)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return value, parsed.timestamp()


def parse_termo_response(response: ProviderResponse) -> TermoToken:
    """Read both documented success formats without relying on the process locale."""
    code = "resposta_termo_invalida"
    if response.status not in {HTTPStatus.OK, HTTPStatus.NOT_MODIFIED}:
        raise TransportFailure(code, response.status)
    try:
        value, expires_at = (
            _cached(response)
            if response.status == HTTPStatus.NOT_MODIFIED
            else _issued(response)
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TransportFailure(code, response.status) from None
    if TOKEN_PATTERN.fullmatch(value) is None:
        code = "resposta_termo_invalida"
        raise TransportFailure(code, response.status)
    return TermoToken(value, expires_at)

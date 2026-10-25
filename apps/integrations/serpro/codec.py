"""Canonical envelopes and minimized parsing at the SERPRO JSON boundary."""

import json
import re
from dataclasses import dataclass, field
from typing import assert_never

from apps.integrations.allowlist import SERPRO_SERVICES
from apps.integrations.transport_types import (
    JSONValue,
    OperacaoNaoPermitida,
    ProviderRequest,
    ProviderResponse,
    TransportFailure,
    decode_json,
)


class CnpjInvalidoError(ValueError):
    """A party cannot be represented by an uppercase alphanumeric CNPJ string."""

    def __init__(self) -> None:
        """Expose no taxpayer value in the refusal."""
        super().__init__("cnpj_invalido")


def _cnpj(value: JSONValue) -> str:
    match value:
        case str():
            normalized = value.upper()
            if re.fullmatch(r"[A-Z0-9]{12}[0-9]{2}", normalized):
                return normalized
            raise CnpjInvalidoError
        case int() | float() | bool() | list() | dict() | None:
            raise CnpjInvalidoError
        case unexpected:
            assert_never(unexpected)


@dataclass(frozen=True, slots=True)
class Parties:
    """The contratante, firm and taxpayer are separate canonical strings."""

    contratante: str
    firm: str
    client: str

    def __post_init__(self) -> None:
        """Normalize once, without converting an identifier into a number."""
        object.__setattr__(self, "contratante", _cnpj(self.contratante))
        object.__setattr__(self, "firm", _cnpj(self.firm))
        object.__setattr__(self, "client", _cnpj(self.client))


def build_request(operation: str, parties: Parties, data: JSONValue) -> ProviderRequest:
    """Build only a registered service's fixed envelope, never a caller-supplied URL."""
    service = SERPRO_SERVICES.get(operation)
    if service is None:
        raise OperacaoNaoPermitida
    envelope = {
        "contratante": {"numero": parties.contratante, "tipo": 2},
        "autorPedidoDados": {"numero": parties.firm, "tipo": 2},
        "contribuinte": {"numero": parties.client, "tipo": 2},
        "pedidoDados": {
            "idSistema": service[0],
            "idServico": service[1],
            "versaoSistema": "1.0",
            "dados": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        },
    }
    return ProviderRequest(
        body=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
    )


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    """Provider contents are transient and excluded from diagnostic representations."""

    status: int
    codes: tuple[str, ...] = field(repr=False)
    data: JSONValue = field(repr=False)


def parse_response(response: ProviderResponse) -> ParsedResponse:
    """Decode the escaped payload without allowing ambiguous JSON or error text out."""
    code = "resposta_serpro_invalida"
    invalid = TransportFailure(code, response.status)
    value = response.json()
    if not isinstance(value, dict):
        raise invalid
    status = value.get("status", response.status)
    messages = value.get("mensagens", [])
    escaped = value.get("dados", "{}")
    if (
        type(status) is not int
        or status != response.status
        or not isinstance(messages, list)
        or not isinstance(escaped, str)
    ):
        raise invalid
    codes: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            raise invalid
        message_code = message.get("codigo")
        if not isinstance(message_code, str):
            raise invalid
        codes.append(message_code)
    try:
        data = decode_json(escaped.encode())
    except (ValueError, RecursionError):
        raise invalid from None
    return ParsedResponse(status, tuple(codes), data)

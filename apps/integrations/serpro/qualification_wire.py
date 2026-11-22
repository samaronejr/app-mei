"""Wire refusals shared by the local SERPRO qualification and test peers."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from apps.integrations.transport_types import decode_json

INVALID_REQUEST: Final = "fake_pedido_invalido"
INVALID_VERSION: Final = "EntradaIncorreta-ICGERENCIADOR-040"


@dataclass(frozen=True, slots=True)
class ExpectedWire:
    """An independent fake oracle; never populated from the send catalog."""

    version: str
    empty_dados: bool = False


def inspect_wire(raw: bytes, expected: Mapping[str, ExpectedWire]) -> tuple[str, str]:
    """Return the service and a refusal code (empty for an accepted envelope)."""
    try:
        envelope = decode_json(raw)
        pedido = envelope.get("pedidoDados") if isinstance(envelope, dict) else None
        if not isinstance(pedido, dict):
            return "", INVALID_REQUEST
        service = pedido.get("idServico")
        if not isinstance(service, str) or service not in expected:
            return "", INVALID_REQUEST
        contract = expected[service]
        if pedido.get("versaoSistema") != contract.version:
            return service, INVALID_VERSION
        dados = pedido.get("dados")
        if not isinstance(dados, str):
            return service, INVALID_REQUEST
        valid = (
            dados == ""
            if contract.empty_dados
            else isinstance(decode_json(dados.encode()), dict)
        )
    except (ValueError, RecursionError):
        return "", INVALID_REQUEST
    return service, "" if valid else INVALID_REQUEST


def refusal_body(code: str) -> bytes:
    """Return a minimized JSON refusal without echoing rejected input."""
    return json.dumps(
        {"status": 400, "mensagens": [{"codigo": code}], "dados": "{}"}
    ).encode()

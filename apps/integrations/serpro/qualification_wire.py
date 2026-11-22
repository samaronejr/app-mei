"""Wire refusals shared by the local SERPRO qualification and test peers."""

import json
from collections.abc import Mapping
from typing import Final

from apps.integrations.transport_types import JSONValue, decode_json

INVALID_REQUEST: Final = "fake_pedido_invalido"
INVALID_VERSION: Final = "EntradaIncorreta-ICGERENCIADOR-040"


# Independent oracle transcribed from the service input tables, not the sender.
# The second fake checks the identifiers and versions against the vendor vectors.
QUALIFICATION_WIRE: Final = {
    "ENVIOXMLASSINADO81": ("AUTENTICAPROCURADOR", "1.0"),
    "SOLICITARPROTOCOLO91": ("SITFIS", "2.0"),
    "RELATORIOSITFIS92": ("SITFIS", "2.0"),
    "PAGAMENTOS71": ("PAGTOWEB", "1.0"),
    "COMPARRECADACAO72": ("PAGTOWEB", "1.0"),
    "GERARDASPDF21": ("PGMEI", "1.0"),
}
_TEXT_INPUT: Final = {
    "ENVIOXMLASSINADO81": "xml",
    "RELATORIOSITFIS92": "protocoloRelatorio",
    "COMPARRECADACAO72": "numeroDocumento",
    "GERARDASPDF21": "periodoApuracao",
}
_PARTIES: Final = ("contratante", "autorPedidoDados", "contribuinte")
_PAYMENT_FILTERS: Final = {
    "numeroDocumentoLista": str,
    "codigoReceitaLista": str,
    "codigoTipoDocumentoLista": int,
}


def _party(value: JSONValue) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("numero"), str)
        and type(value.get("tipo")) is int
        and value["tipo"] in (1, 2)
    )


def _interval(value: JSONValue, keys: tuple[str, str], types: tuple[type, ...]) -> bool:
    return isinstance(value, dict) and all(
        key in keys and type(item) in types for key, item in value.items()
    )


def _payment_input(data: dict[str, JSONValue]) -> bool:
    if not all(
        type(data.get(key)) is int for key in ("primeiroDaPagina", "tamanhoDaPagina")
    ):
        return False
    for key, item in data.items():
        if key in _PAYMENT_FILTERS and (
            not isinstance(item, list)
            or any(type(value) is not _PAYMENT_FILTERS[key] for value in item)
        ):
            return False
        if key == "intervaloDataArrecadacao" and not _interval(
            item, ("dataInicial", "dataFinal"), (str,)
        ):
            return False
        if key == "intervaloValorTotalDocumento" and not _interval(
            item, ("valorInicial", "valorFinal"), (int, float)
        ):
            return False
    return True


def _dados_valid(service: str, dados: str) -> bool:
    if service == "SOLICITARPROTOCOLO91":
        return dados == ""
    data = decode_json(dados.encode())
    if not isinstance(data, dict):
        return False
    if service == "PAGAMENTOS71":
        return _payment_input(data)
    if not isinstance(data.get(_TEXT_INPUT[service]), str):
        return False
    return (
        service != "GERARDASPDF21"
        or "dataConsolidacao" not in data
        or isinstance(data["dataConsolidacao"], str)
    )


def inspect_wire(
    raw: bytes, expected: Mapping[str, tuple[str, str]]
) -> tuple[str, str]:
    """Return the service and a refusal code (empty for an accepted envelope)."""
    try:
        envelope = decode_json(raw)
        if not isinstance(envelope, dict) or not all(
            _party(envelope.get(key)) for key in _PARTIES
        ):
            return "", INVALID_REQUEST
        pedido = envelope.get("pedidoDados")
        service = pedido.get("idServico") if isinstance(pedido, dict) else None
        if not isinstance(service, str) or service not in expected:
            return "", INVALID_REQUEST
        system, version = expected[service]
        if not isinstance(pedido, dict) or pedido.get("idSistema") != system:
            return service, INVALID_REQUEST
        if pedido.get("versaoSistema") != version:
            return service, INVALID_VERSION
        dados = pedido.get("dados")
        valid = isinstance(dados, str) and _dados_valid(service, dados)
    except (ValueError, RecursionError):
        return "", INVALID_REQUEST
    return service, "" if valid else INVALID_REQUEST


def refusal_body(code: str) -> bytes:
    """Return a minimized JSON refusal without echoing rejected input."""
    return json.dumps(
        {"status": 400, "mensagens": [{"codigo": code}], "dados": "{}"}
    ).encode()

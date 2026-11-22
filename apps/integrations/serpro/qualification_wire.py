"""Validate loopback requests against the packaged, independent vendor vectors."""

import base64
import binascii
import json
import re
from functools import cache
from pathlib import Path
from typing import Any, Final, TypeGuard

from apps.integrations.transport_types import JSONValue, decode_json

INVALID_REQUEST: Final = "fake_pedido_invalido"
INVALID_VERSION: Final = "EntradaIncorreta-ICGERENCIADOR-040"
# The Docker image includes the tracked vectors; the CLI imports no test code.
_VECTORS = Path(__file__).parents[3] / "tests/fixtures/providers/serpro/wire"
_PARTY_NUMBER: Final = {1: r"[0-9]{11}", 2: r"[A-Z0-9]{12}[0-9]{2}"}
_FORMATS: Final = {
    "period": r"[0-9]{6}",
    "compact-date": r"[0-9]{8}",
    "date": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
}
_WHOLE_RANGES: Final = {
    "document-type": (0, 99),
    "page-offset": (0, None),
    "page-size": (1, 100),
}
_DOCUMENT_BYTES = 17
_REVENUE_POSITIONS = 4


@cache
def _contracts() -> dict[tuple[str, str], Any]:
    contracts = {}
    for path in _VECTORS.glob("*.json"):
        vector = json.loads(path.read_text())
        pedido = vector["body"]["pedidoDados"]
        contracts[pedido["idSistema"], pedido["idServico"]] = vector
    return contracts


def _party(value: JSONValue, allowed: list[int]) -> bool:
    if not isinstance(value, dict):
        return False
    number, kind = value.get("numero"), value.get("tipo")
    return (
        isinstance(number, str)
        and type(kind) is int
        and kind in allowed
        and re.fullmatch(_PARTY_NUMBER[kind], number) is not None
    )


def _whole(value: JSONValue) -> TypeGuard[int | float]:
    return type(value) is int or (type(value) is float and value.is_integer())


def _base64(value: JSONValue) -> bool:
    try:
        return isinstance(value, str) and bool(base64.b64decode(value, validate=True))
    except (ValueError, binascii.Error):
        return False


def _primitive(value: JSONValue, rule: str) -> bool:
    if rule in _WHOLE_RANGES:
        minimum, maximum = _WHOLE_RANGES[rule]
        return (
            _whole(value) and value >= minimum and (maximum is None or value <= maximum)
        )
    match rule:
        case "text":
            return isinstance(value, str) and bool(value.strip())
        case "base64":
            return _base64(value)
        case "document-number":
            return isinstance(value, str) and 0 < len(value.encode()) <= _DOCUMENT_BYTES
        case "revenue-code":
            return (
                isinstance(value, str) and 0 < len(value) <= _REVENUE_POSITIONS
            ) or (_whole(value) and 0 <= value < 10**_REVENUE_POSITIONS)
        case _:
            return (
                type(value) in (int, float)
                if rule == "number"
                else isinstance(value, str)
                and rule in _FORMATS
                and re.fullmatch(_FORMATS[rule], value) is not None
            )


def _matches(value: JSONValue, rule: JSONValue) -> bool:
    if isinstance(rule, list):
        return isinstance(value, list) and all(
            _matches(item, rule[0]) for item in value
        )
    if isinstance(rule, dict):
        return isinstance(value, dict) and all(
            key not in value or _matches(value[key], child)
            for key, child in rule.items()
        )
    return isinstance(rule, str) and _primitive(value, rule)


def _dados_valid(dados: JSONValue, rule: dict[str, Any]) -> bool:
    if not isinstance(dados, str):
        return False
    if rule["form"] == "empty":
        return dados == ""
    parsed = decode_json(dados)
    return (
        isinstance(parsed, dict)
        and all(
            key in parsed and _matches(parsed[key], spec)
            for key, spec in rule["required"].items()
        )
        and _matches(parsed, rule["optional"])
    )


def inspect_wire(
    method: str, path: str, raw: bytes, *, trial: bool = False
) -> tuple[str, str]:
    """Return (service, refusal code) without consulting the sender or reply queue."""
    try:
        # The text parser rejects a BOM and cannot auto-detect UTF-16/32.
        envelope = decode_json(raw.decode("utf-8"))
        pedido = envelope.get("pedidoDados") if isinstance(envelope, dict) else None
        if not isinstance(pedido, dict):
            return "", INVALID_REQUEST
        system, service = pedido.get("idSistema"), pedido.get("idServico")
        if not isinstance(system, str) or not isinstance(service, str):
            return "", INVALID_REQUEST
        vector = _contracts().get((system, service))
        if vector is None or [method, path] != vector["route"]:
            return "", INVALID_REQUEST
        version = pedido.get("versaoSistema")
        expected = vector["body"]["pedidoDados"]["versaoSistema"]
        if trial:
            expected = vector.get("trial_version", expected)
        if isinstance(version, str) and version != expected:
            return service, INVALID_VERSION
        valid = (
            isinstance(envelope, dict)
            and isinstance(version, str)
            and all(
                _party(envelope.get(key), allowed)
                for key, allowed in vector["parties"].items()
            )
            and _dados_valid(pedido.get("dados"), vector["dados"])
        )
    except (ValueError, RecursionError):
        return "", INVALID_REQUEST
    return service, "" if valid else INVALID_REQUEST


def refusal_body(code: str) -> bytes:
    """Return a minimized JSON refusal without echoing rejected input."""
    return json.dumps(
        {"status": 400, "mensagens": [{"codigo": code}], "dados": "{}"}
    ).encode()

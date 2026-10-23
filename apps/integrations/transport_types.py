"""Ephemeral provider wire values; never persisted or rendered as diagnostics."""

import json
from dataclasses import dataclass, field
from typing import Literal, Never

from apps.integrations.call_contracts import CallResult

type JSONValue = (
    str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None
)
type Provider = Literal["serpro", "focus"]
type Environment = Literal["fake", "trial", "homologacao", "producao"]


def _unique_object(items: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result = dict(items)
    if len(result) != len(items):
        msg = "duplicate_json_key"
        raise ValueError(msg)
    return result


def _reject_constant(_value: str) -> Never:
    msg = "non_json_number"
    raise ValueError(msg)


def decode_json(data: bytes) -> JSONValue:
    """Reject ambiguous objects and non-JSON numbers at the wire boundary."""
    value: JSONValue = json.loads(
        data, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    return value


class OperacaoNaoPermitidaError(Exception):
    """A request does not match the closed provider operation register."""

    def __init__(self) -> None:
        """Expose only the catalog refusal code."""
        super().__init__("operacao_nao_permitida")


OperacaoNaoPermitida = OperacaoNaoPermitidaError


class TransportFailureError(Exception):
    """Only a symbolic code/status escape; underlying URLs and bodies do not."""

    def __init__(self, code: str, status: int | None = None) -> None:
        """Keep diagnostic values separate from untrusted provider text."""
        self.code = code
        self.status = status
        super().__init__(code)

    def result(self) -> CallResult:
        """Preserve uncertain execution/billing; an unreadable response is failure."""
        return CallResult(
            "incerta" if self.status is None else "falhou",
            self.status,
            self.code,
            "desconhecido",
        )


TransportFailure = TransportFailureError


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """No URL or HTTP method supplied by the caller; routes come from the catalog."""

    variant: str = "default"
    subject: str = ""
    query: tuple[tuple[str, str], ...] = ()
    body: bytes = field(default=b"", repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Bounded bytes plus safe metadata, without Location or raw request handles."""

    status: int
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...] = ()

    def json(self) -> JSONValue:
        """Parse only already-bounded bytes, without echoing invalid contents."""
        try:
            value = decode_json(self.body)
        except (ValueError, RecursionError):
            msg = "resposta_invalida"
            raise TransportFailure(msg, self.status) from None
        return value

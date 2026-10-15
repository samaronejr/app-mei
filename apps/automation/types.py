"""Typed values crossing the provider boundary."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TypedDict

type JSONValue = (
    bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
)
type JSONObject = dict[str, JSONValue]


class RunState(StrEnum):
    """Private reservation lifecycle, not an onboarding or review status."""

    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"
    PROVIDER_ERROR = "provider_error"
    DISCARDED = "discarded"


class DocType(StrEnum):
    """Provider document categories, not verification or fiscal regime states."""

    PRODUCT_INVOICE = "product_invoice"
    SERVICE_INVOICE = "service_invoice"
    DAS = "das"
    STATEMENT = "statement"
    RECEIPT = "receipt"
    UNKNOWN = "unknown"


class Message(TypedDict):
    """Only system/user text; there is no tool-message capability."""

    role: str
    content: str


class ProviderError(Exception):
    """A sanitized code, never a provider body, URL or raw prompt."""

    def __init__(self, code: str) -> None:
        """Preserve only the machine-readable failure."""
        self.code = code
        super().__init__(code)


class UnsafeOutputError(Exception):
    """The output must not be persisted or used."""


@dataclass(frozen=True, slots=True)
class Budget:
    """Maximum reservation, not a claim about provider billing."""

    max_brl: Decimal = Decimal("0.025")
    max_tokens: int = 512


@dataclass(frozen=True, slots=True)
class PreparedCall:
    """Every local check passed and the request is built; only dispatch remains."""

    schema: JSONObject = field(repr=False)
    messages: tuple[Message, ...] = field(repr=False)
    budget: Budget
    body: bytes = field(default=b"", repr=False)
    input_brl_per_million: Decimal = Decimal(0)
    output_brl_per_million: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class Result:
    """Untrusted provider content plus measured or fixture usage."""

    output: str
    model_id: str = "fake-v1"
    cost_brl: Decimal = Decimal("0.001")
    latency_ms: int = 1


@dataclass(frozen=True, slots=True)
class Field:
    """A proposal with a source span, never a deterministic field write."""

    name: str
    value: str
    source_span: str

    def as_json(self) -> JSONObject:
        """Serialize the validated field."""
        return {"name": self.name, "value": self.value, "source_span": self.source_span}


@dataclass(frozen=True, slots=True)
class Classification:
    """Schema-validated classification; server context owns all IDs."""

    doc_type: str
    fields: tuple[Field, ...]
    client_cnpj_seen: str
    confidence_band: str

    def as_json(self) -> JSONObject:
        """Serialize for private storage."""
        return {
            "doc_type": self.doc_type,
            "fields": [field.as_json() for field in self.fields],
            "client_cnpj_seen": self.client_cnpj_seen,
            "confidence_band": self.confidence_band,
        }

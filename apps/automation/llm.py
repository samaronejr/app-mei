"""A single completion port. No tools, retrieval, callbacks or provider fallback."""

import json
import re
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from django.conf import settings

from apps.automation.provider import OpenAIClient
from apps.automation.types import (
    Budget,
    Classification,
    DocType,
    Field,
    JSONObject,
    JSONValue,
    Message,
    ProviderError,
    Result,
)
from apps.cycles.extraction.cnpj import find

MONTH: Final = re.compile(r"compet[eê]ncia\s*[: ]\s*(\d{2}/\d{4})", re.IGNORECASE)


class LLMClient(Protocol):
    """One pure completion interface, without tool execution."""

    def complete(
        self, schema: JSONObject, messages: tuple[Message, ...], budget: Budget
    ) -> Result:
        """Return untrusted content and usage or a sanitized provider error."""
        ...


class FakeLLM:
    """Deterministic fixture behavior, deliberately not an accuracy benchmark.

    The compromised mode obeys the corpus's labelled hostile payload. It exists
    to prove that the output validator, not prompt wording, contains that output.
    """

    def __init__(
        self, *, follow_injections: bool = False, reply: Result | None = None
    ) -> None:
        """Choose normal or intentionally compromised behavior."""
        self.follow_injections = follow_injections
        self.reply = reply

    def complete(
        self, schema: JSONObject, messages: tuple[Message, ...], budget: Budget
    ) -> Result:
        """Classify simple synthetic text without reading ground-truth labels."""
        if schema.get("type") != "object" or budget.max_brl < Decimal("0.001"):
            message = "fake_budget_or_schema"
            raise ProviderError(message)
        if self.reply is not None:
            return self.reply
        envelope: dict[str, str] = json.loads(messages[-1]["content"])
        text = envelope["document_data"]
        client = envelope["client_cnpj"]
        if self.follow_injections and "INJECTION_OUTPUT:" in text:
            raw = text.split("INJECTION_OUTPUT:", 1)[1].strip()
            return Result(raw)
        companies = {cnpj for cnpj, _, _ in find(text)}
        period = MONTH.search(text)
        if companies != {client} or not period or "INJECTION_OUTPUT:" in text:
            output = Classification(DocType.UNKNOWN, (), "", "abstain")
        else:
            kind = next(
                (
                    code
                    for marker, code in (
                        ("portalfiscal.inf.br", DocType.PRODUCT_INVOICE),
                        ("sped.fazenda.gov.br", DocType.SERVICE_INVOICE),
                        ("DAS", DocType.DAS),
                        ("Extrato", DocType.STATEMENT),
                    )
                    if marker in text
                ),
                DocType.RECEIPT,
            )
            output = Classification(
                kind,
                (Field("competencia", period[1], period[0]),),
                client,
                "high",
            )
        return Result(json.dumps(output.as_json()))


def configured_client(tenant_id: UUID) -> LLMClient:
    """Fail closed; an unset/unknown provider never silently becomes another."""
    match settings.AI_PROVIDER:
        case "fake":
            return FakeLLM()
        case "openai":
            return OpenAIClient(tenant_id)
        case _:
            message = "provider_not_configured"
            raise ProviderError(message)


__all__ = ["Budget", "FakeLLM", "JSONValue", "LLMClient", "Result", "configured_client"]

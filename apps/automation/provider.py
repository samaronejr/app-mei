"""Candidate OpenAI adapter; OD-05 approval is a separate, default-closed gate.

API: https://platform.openai.com/docs/api-reference/chat/create
One bounded HTTPS POST, no redirects, retries, tools or fallback. Requests set
store=false; provider retention still requires the separate OD-05 agreement.
"""

import json
from contextlib import closing
from decimal import Decimal
from http.client import HTTPException, HTTPSConnection
from time import monotonic
from typing import Final
from uuid import UUID

from django.conf import settings

from apps.automation.types import (
    Budget,
    JSONObject,
    JSONValue,
    Message,
    ProviderError,
    Result,
)
from apps.core.flags import tenant_has
from apps.core.tasks import assert_no_transaction

MAX_RESPONSE_BYTES: Final = 65536
HTTP_OK: Final = 200
MILLION: Final = Decimal(1000000)


def _object(value: JSONValue) -> JSONObject:
    if not isinstance(value, dict):
        message = "provider_response_invalid"
        raise ProviderError(message)
    return value


def _tokens(value: JSONValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        message = "provider_response_invalid"
        raise ProviderError(message)
    return value


def _completion(data: bytes) -> tuple[str, int, int]:
    try:
        parsed = _object(json.loads(data))
    except (ValueError, RecursionError) as error:
        message = "provider_response_invalid"
        raise ProviderError(message) from error
    choices = parsed.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        message = "provider_response_invalid"
        raise ProviderError(message)
    choice = _object(choices[0])
    reply = _object(choice.get("message"))
    content = reply.get("content")
    if (
        choice.get("finish_reason") != "stop"
        or reply.get("tool_calls")
        or reply.get("function_call")
        or reply.get("refusal")
        or not isinstance(content, str)
    ):
        message = "provider_refusal"
        raise ProviderError(message)
    usage = _object(parsed.get("usage"))
    return (
        content,
        _tokens(usage.get("prompt_tokens")),
        _tokens(usage.get("completion_tokens")),
    )


class OpenAIClient:
    """Transport-only adapter; never an owner provider/DPA decision."""

    def complete(
        self, schema: JSONObject, messages: tuple[Message, ...], budget: Budget
    ) -> Result:
        """Call the fixed provider host after checking the conservative cost bound."""
        assert_no_transaction()
        if (
            settings.AI_GLOBAL_DISABLED
            or settings.AI_PROVIDER != "openai"
            or not settings.AI_REAL_DATA_APPROVED
            or not settings.AI_API_KEY
            or not settings.AI_MODEL
            or not tenant_has(self.tenant_id, "ai_shadow")
        ):
            message = "provider_gate_closed"
            raise ProviderError(message)
        input_rate = Decimal(settings.AI_INPUT_BRL_PER_MILLION)
        output_rate = Decimal(settings.AI_OUTPUT_BRL_PER_MILLION)
        if (
            not input_rate.is_finite()
            or not output_rate.is_finite()
            or input_rate <= 0
            or output_rate <= 0
        ):
            message = "pricing_required"
            raise ProviderError(message)
        payload = json.dumps(
            {
                "model": settings.AI_MODEL,
                "messages": messages,
                "max_completion_tokens": budget.max_tokens,
                "store": False,
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "doc_classify_v1",
                        "strict": True,
                        "schema": schema,
                    },
                },
            },
            ensure_ascii=False,
        ).encode()
        # One token per UTF-8 byte plus protocol overhead is conservative for the
        # configured tokenizer. Pricing/FX is an operator-supplied upper bound.
        ceiling = (
            (len(payload) + 1024) * input_rate + budget.max_tokens * output_rate
        ) / MILLION
        if ceiling > budget.max_brl:
            message = "call_budget_exceeded"
            raise ProviderError(message)
        started = monotonic()
        try:
            with closing(HTTPSConnection("api.openai.com", timeout=15)) as transport:
                transport.request(
                    "POST",
                    "/v1/chat/completions",
                    payload,
                    {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {settings.AI_API_KEY}",
                    },
                )
                response = transport.getresponse()
                if response.status != HTTP_OK:
                    message = "provider_http_error"
                    raise ProviderError(message)
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    message = "provider_response_too_large"
                    raise ProviderError(message)
        except (OSError, HTTPException) as error:
            message = "provider_unavailable"
            raise ProviderError(message) from error
        content, input_tokens, output_tokens = _completion(data)
        cost = (input_tokens * input_rate + output_tokens * output_rate) / MILLION
        if not cost.is_finite() or cost < 0 or cost > budget.max_brl:
            message = "provider_usage_invalid"
            raise ProviderError(message)
        return Result(
            content,
            str(settings.AI_MODEL),
            cost,
            round((monotonic() - started) * 1000),
        )

    def __init__(self, tenant_id: UUID) -> None:
        """Bind the configured call to an explicitly enabled firm."""
        self.tenant_id = tenant_id

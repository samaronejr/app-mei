"""Ciencia-only wire results and attributable, private NF-e state read-backs."""

from __future__ import annotations

import re
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING

from apps.integrations.call_models import CallState
from apps.integrations.focus.adapter import classify
from apps.integrations.transport_types import TransportFailure, decode_json

if TYPE_CHECKING:
    from apps.integrations.call_contracts import CallResult
    from apps.integrations.transport_types import ProviderResponse


def _registration_result(
    response: ProviderResponse | TransportFailure,
) -> CallResult:
    """Settle only a complete synchronous event response with consistent fields."""
    if isinstance(response, TransportFailure):
        return replace(response.result(), state=CallState.UNCERTAIN)
    result = classify("POST", "registration", response)
    if response.status != HTTPStatus.OK:
        return replace(
            result, state=CallState.UNCERTAIN, provider_code="evento_sem_confirmacao"
        )
    try:
        data = decode_json(response.body)
    except (ValueError, RecursionError):
        return replace(
            result, state=CallState.UNCERTAIN, provider_code="resposta_invalida"
        )
    state, code = CallState.UNCERTAIN, "evento_sem_confirmacao"
    if isinstance(data, dict) and data.get("tipo") == "ciencia":
        status = data.get("status_sefaz")
        protocol = data.get("protocolo")
        if isinstance(status, str):
            if (
                data.get("status") == "evento_registrado"
                and status in {"135", "136"}
                and isinstance(protocol, str)
                and re.fullmatch(r"[0-9]{15}|[0-9]{17}", protocol) is not None
                and set(protocol) != {"0"}
            ):
                return result
            if data.get("status") == "erro":
                if status == "573":
                    # The same SEFAZ event identity may already exist; exact GET
                    # must establish its state rather than settling this POST.
                    code = "evento_duplicado"
                # MOC recipient-event refusals; batch processing (128) is not one.
                elif (
                    status in {"496", "594", "596", "650", "655", "658"}
                    and protocol is None
                ):
                    state, code = CallState.FAILED, "evento_recusado"
    return replace(result, state=state, provider_code=code)

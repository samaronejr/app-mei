"""Shared operator-command output: JSON lines and the `.evidence` receipt.

The receipt lists the fields changed, the journaled call ids and the outcome;
it never holds a token, an Authorization header or a provider reply.
"""

import json
from pathlib import Path

from django.core.management.base import OutputWrapper
from django.utils import timezone

from apps.integrations.budget_pricing import BudgetDeniedError
from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.focus.company import ConfigurationOutcome
from apps.integrations.privacy import scrub_provider_text


def emit(stream: OutputWrapper, record: dict[str, object]) -> None:
    """One sorted JSON object per line."""
    stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def outcome_record(outcome: ConfigurationOutcome) -> dict[str, object]:
    """Minimize the result of one setup or disable for output and receipts."""
    return {
        "write_state": outcome.write.state if outcome.write else "",
        "write_code": outcome.write.provider_code if outcome.write else "",
        "readback_state": outcome.readback.state if outcome.readback else "",
        "confirmed": outcome.confirmed,
        "unconfirmed": outcome.unconfirmed,
        "calls": outcome.call_ids,
        "link_status": outcome.link_status,
        "disablement_state": outcome.disablement_state,
        "credential_state": outcome.credential_state,
    }


def admission_message(error: AdmissionDeniedError) -> str:
    """Explain in pt-BR why the protocol refused before sending anything.

    Request keys embed the request digest and a per-invocation occurrence, so an
    `IntentConflictError` (same key, other bytes) cannot arise from these commands.
    """
    if isinstance(error, BudgetDeniedError):
        return f"Sem orçamento Focus para o mês ({error.reason}); nada foi enviado."
    return f"Chamada recusada pela admissão ({error}); nada foi enviado."


def write_receipt(kind: str, record: dict[str, object]) -> Path:
    """Write `.evidence/AXRM-FOCUS-<kind>-<stamp>.txt` in the working directory."""
    destination = Path(".evidence")
    destination.mkdir(exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%dT%H%M%S%fZ")
    path = destination / f"AXRM-FOCUS-{kind}-{stamp}.txt"
    body = json.dumps(
        {"command": f"focus_company_{kind}", "recorded_at": stamp, **record},
        sort_keys=True,
        ensure_ascii=False,
    )
    path.write_text(scrub_provider_text(body) + "\n")
    return path

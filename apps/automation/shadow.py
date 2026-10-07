"""Claim -> read/provider outside transactions -> finalize private observations."""

import hashlib
import json
from decimal import Decimal
from typing import Final
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.automation.admission import Ticket, claim, eligible, enabled
from apps.automation.classification import messages_for, schema, validate_output
from apps.automation.llm import configured_client
from apps.automation.models import AgentRun
from apps.automation.types import (
    Classification,
    DocType,
    JSONObject,
    ProviderError,
    Result,
    RunState,
    UnsafeOutputError,
)
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.extraction.cnpj import find
from apps.cycles.parsing import UnsafeDocument, parse_intake_pdf, parse_intake_xml
from apps.cycles.sniffing import PDF, XML

MAX_TEXT: Final = 20000


def source_text(ticket: Ticket) -> str:
    """Read only this clean source, verify its hash, and respect T12 parser bounds."""
    assert_no_transaction()
    with default_storage.open(ticket.intake.document.storage_key, "rb") as handle:
        payload = handle.read()
    if hashlib.sha256(payload).hexdigest() != ticket.intake.document.sha256:
        message = "source_integrity"
        raise ProviderError(message)
    match ticket.intake.sniffed_type:
        case value if value == PDF:
            text = parse_intake_pdf(ticket.intake, payload)
        case value if value == XML:
            parse_intake_xml(ticket.intake, payload)
            text = payload.decode("utf-8-sig")
        case _:
            text = ""
    if len(text) > MAX_TEXT:
        message = "source_too_large"
        raise ProviderError(message)
    if not enabled(ticket.tenant_id) or not eligible(
        ticket.tenant_id, ticket.intake.pk
    ):
        message = "admission_changed"
        raise ProviderError(message)
    return text


def run_shadow(tenant_id: UUID, intake_id: UUID) -> str:
    """Failures are private records; never change deterministic fields or intake."""
    assert_no_transaction()
    ticket = claim(tenant_id, intake_id, now=timezone.now())
    if ticket is None:
        return "skipped"
    output: JSONObject = {}
    state, error = RunState.COMPLETED, ""
    cost = None
    latency = 0
    try:
        text = source_text(ticket)
        assert_no_transaction()
        companies = {cnpj for cnpj, _, _ in find(text)}
        if not text.strip() or companies != {ticket.client_cnpj}:
            result = Result(
                json.dumps(
                    Classification(DocType.UNKNOWN, (), "", "abstain").as_json()
                ),
                cost_brl=Decimal(0),
                latency_ms=0,
            )
        else:
            result = configured_client(tenant_id).complete(
                schema(),
                messages_for(ticket.client_cnpj, text, ticket.expected_documents),
                ticket.budget,
            )
        cost, latency = result.cost_brl, result.latency_ms
        output = validate_output(result.output, ticket.client_cnpj).as_json()
    except UnsafeOutputError:
        state, error = RunState.REJECTED, "unsafe_output"
    except ProviderError as failure:
        state, error = RunState.PROVIDER_ERROR, failure.code
    except (OSError, UnicodeError, UnsafeDocument):
        state, error = RunState.PROVIDER_ERROR, "source_unreadable"
    except SoftTimeLimitExceeded:
        state, error = RunState.PROVIDER_ERROR, "provider_timeout"
    with tenant_context(tenant_id):
        run = AgentRun.objects.select_for_update().get(pk=ticket.run_id)
        if run.state != RunState.PENDING:
            return str(run.state)
        if run.document_id is None or run.expires_at <= timezone.now():
            output = {}
            state, error = RunState.DISCARDED, "source_removed_or_expired"
        run.state, run.error_code, run.output = state, error, output
        run.cost_brl, run.latency_ms = cost, latency
        run.finalized_at = timezone.now()
        run.save(
            update_fields=[
                "state",
                "error_code",
                "output",
                "cost_brl",
                "latency_ms",
                "finalized_at",
            ]
        )
    return state

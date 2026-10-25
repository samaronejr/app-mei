"""Immutable per-attempt receipts and usage; no outcome-precedence aggregation."""

from datetime import datetime
from uuid import UUID, uuid5

from apps.integrations.call_contracts import CallResult, IntentConflictError
from apps.integrations.call_models import ProviderCall, ProviderCallAttempt
from apps.tenants.models import UsageKind, UsageRecord


def observe(
    call: ProviderCall, attempt: ProviderCallAttempt, result: CallResult
) -> bool:
    """Insert an observation once, refusing a conflicting immutable replay."""
    observed, created = ProviderCallAttempt.objects.get_or_create(
        tenant_id=call.tenant_id,
        client_id=call.client_id,
        call=call,
        attempt_id=attempt.attempt_id,
        observation=attempt.observation,
        defaults={
            "attempt_number": attempt.attempt_number,
            "fence": attempt.fence,
            "outcome": result.state,
            "http_status": result.http_status,
            "provider_code": result.provider_code,
            "billed": result.billed,
            "response_digest": result.response_digest,
            "reserved_microbrl": attempt.reserved_microbrl,
            "settled_microbrl": result.settled_microbrl,
            "occurred_at": attempt.occurred_at,
        },
    )
    if not created and (
        observed.outcome != result.state
        or observed.http_status != result.http_status
        or observed.provider_code != result.provider_code
        or observed.billed != result.billed
        or observed.response_digest != result.response_digest
        or observed.fence != attempt.fence
    ):
        raise IntentConflictError
    return created


def meter(
    call: ProviderCall, attempt_id: UUID, result: CallResult, occurred_at: datetime
) -> None:
    """Rebuild a billed attempt without charging it again on repeated replay."""
    metric = {
        ("serpro", "consulta"): "serpro_consulta",
        ("serpro", "emissao"): "serpro_emissao",
    }.get((call.provider, call.billing_class)) or {
        ("focus", "registration"): "focus_eventos",
    }.get((call.provider, call.operation_class))
    if metric is None:
        return
    existing = UsageRecord.objects.filter(
        metric=metric, source_event=attempt_id
    ).first()
    if result.billed == "nao" and existing is None:
        return
    quantity = int(result.billed != "nao")
    kind = (
        UsageKind.ESTIMATED if result.billed == "desconhecido" else UsageKind.CONFIRMED
    )
    if existing is not None:
        if existing.kind == UsageKind.ESTIMATED and kind == UsageKind.CONFIRMED:
            UsageRecord.objects.get_or_create(
                tenant_id=call.tenant_id,
                metric=metric,
                source_event=uuid5(attempt_id, "provider-result"),
                defaults={
                    "period": occurred_at.date(),
                    "quantity": quantity,
                    "kind": kind,
                    "correction_of": existing,
                },
            )
        return
    UsageRecord.objects.create(
        tenant_id=call.tenant_id,
        metric=metric,
        source_event=attempt_id,
        period=occurred_at.date(),
        quantity=quantity,
        kind=kind,
    )

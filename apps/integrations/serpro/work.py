"""Route minimized SERPRO failures to owned client or firm work."""

from datetime import date
from typing import Final
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.core.flags import tenant_has
from apps.core.tenancy import tenant_context
from apps.cycles.openers import fallback_owner
from apps.cycles.services.exceptions import ExceptionReason
from apps.integrations.authority import Problem, raise_problem
from apps.integrations.call_contracts import Claim
from apps.integrations.call_models import CallState, FirmProviderCall, ProviderCall
from apps.integrations.models import (
    ClientProviderLink,
    FirmAuthorization,
    FirmAuthorizationStatus,
    ProviderConnection,
)
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.termo_state import termo_head, termo_in_force
from apps.tenants.models import Tenant

AUTH_FAILED: Final = "autenticacao_serpro_recusada"
PROC_MISSING: Final = "autorizacao_procurador_ausente"
UNCERTAIN: Final = "chamada_provedor_incerta"
IN_FLIGHT: Final = frozenset(
    {
        CallState.QUEUED,
        CallState.RESERVED,
        CallState.CLAIMED,
        CallState.STARTED,
        CallState.RETRY,
    }
)
REASONS: Final = {
    AUTH_FAILED: ExceptionReason(
        "Confira as credenciais do SERPRO. A segunda recusa de autenticacao "
        "encerrou a chamada; nenhuma terceira tentativa foi enviada.",
        label="autenticacao SERPRO recusada",
    ),
    PROC_MISSING: ExceptionReason(
        "Confira a validade do termo assinado do escritorio e reenvie-o pelo "
        "fluxo de autorizacao antes de retomar as consultas.",
        label="autorizacao do procurador ausente",
    ),
}


def own_client_result(ticket: Claim) -> None:
    """Only finalized SERPRO conditions open work, never arbitrary provider prose."""
    with tenant_context(ticket.tenant_id):
        call = ProviderCall.objects.select_related("client").get(
            pk=ticket.call_id, client_id=ticket.client_id
        )
        if call.provider != "serpro":
            return
        reason = ""
        if call.http_status == 401 and call.attempts == 2:  # noqa: PLR2004
            reason = AUTH_FAILED
        elif call.provider_code == "AcessoNegado-ICGERENCIADOR-016":
            reason = PROC_MISSING
        if reason:
            raise_problem(
                call.client,
                Problem(
                    reason,
                    call.pk,
                    review_on=timezone.localdate(),
                    service=call.operation,
                ),
            )
            if reason == PROC_MISSING:
                own_firm_condition(call.link.connection, reason, [str(call.pk)])


def _active_issue(connection_id: UUID, reason: str) -> FirmIntegrationIssue | None:
    return (
        FirmIntegrationIssue.objects.select_for_update()
        .filter(connection_id=connection_id, reason=reason, state__in=ACTIVE_STATES)
        .first()
    )


def _converge(
    tenant_id: UUID,
    connection_id: UUID,
    reason: str,
    references: list[str],
    call: FirmProviderCall | None = None,
) -> None:
    """Open one active issue per connection and reason, or extend its evidence.

    A concurrent opener that commits first wins the partial unique index, and the
    loser joins that row, so the owned work converges instead of duplicating.
    """
    issue = _active_issue(connection_id, reason)
    if issue is None:
        owner = fallback_owner(tenant_id)
        if owner is None:
            return
        try:
            with transaction.atomic():
                issue = FirmIntegrationIssue.objects.create(
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    call=call,
                    reason=reason,
                    owner=owner,
                    review_on=timezone.localdate(),
                    evidence_refs=references,
                )
        except IntegrityError:
            issue = _active_issue(connection_id, reason)
            if issue is None:
                raise
        else:
            metadata: dict[str, object] = {"reason": reason}
            if call is not None:
                metadata["call"] = str(call.pk)
            record_event(
                action="integrations.firm_issue_opened",
                tenant_id=tenant_id,
                obj=ObjectRef("FirmIntegrationIssue", str(issue.pk)),
                metadata=metadata,
            )
            return
    additions = [value for value in references if value not in issue.evidence_refs]
    if additions:
        issue.evidence_refs = [*issue.evidence_refs, *additions]
        issue.version += 1
        issue.updated_at = timezone.now()
        issue.save(update_fields=["evidence_refs", "version", "updated_at"])


def own_firm_condition(
    connection: ProviderConnection, reason: str, references: list[str]
) -> None:
    """Own expiry or a missing cached authorization even before a firm call exists."""
    _converge(connection.tenant_id, connection.pk, reason, references)


def own_firm_result(call: FirmProviderCall) -> None:
    """Converge on firm-owned recovery without a client column or hidden dummy row."""
    reason = ""
    if call.state == CallState.UNCERTAIN:
        reason = UNCERTAIN
    elif call.http_status == 401 and call.attempts == 2:  # noqa: PLR2004
        reason = AUTH_FAILED
    elif call.provider_code == "AcessoNegado-ICGERENCIADOR-016":
        reason = PROC_MISSING
    if not reason:
        return
    _converge(call.tenant_id, call.connection_id, reason, [str(call.pk)], call)


def _settled(submission: FirmAuthorization) -> bool:
    return (
        FirmAuthorization.objects.filter(
            signed_document_id=submission.signed_document_id,
            submitted_at=submission.submitted_at,
        )
        .exclude(status=FirmAuthorizationStatus.ENVIADO)
        .exists()
    )


def _own_unsettled_submission(connection: ProviderConnection) -> None:
    """Own every termo submission that reached SERPRO but never settled.

    `termo_head` ignores `enviado` rows, so a submission whose call is final, or
    whose dead worker's lease ran out, would otherwise stay unowned, even while an
    older termo keeps the connection in force. Only a call that began dispatch
    (`attempts > 0`) counts: one never sent has no reply to own, and a later retry
    refused or abandoned before its first byte must not hide an earlier one that
    was sent. A submission older than the latest settled outcome is superseded:
    its reply can no longer change which termo is in force.
    """
    calls = (
        FirmProviderCall.objects.filter(connection=connection, attempts__gt=0)
        .select_related("authorization")
        .order_by("authorization__submitted_at", "pk")
    )
    head = termo_head(connection.pk)
    if head is not None:
        calls = calls.filter(authorization__submitted_at__gte=head.submitted_at)
    now = timezone.now()
    unsettled = [
        call
        for call in calls
        if not (
            call.state in IN_FLIGHT
            and (call.lease_until is None or call.lease_until > now)
        )
        and not _settled(call.authorization)
    ]
    if unsettled:
        _converge(
            connection.tenant_id,
            connection.pk,
            UNCERTAIN,
            [str(call.pk) for call in unsettled],
            unsettled[0],
        )


def evaluate_termo_conditions(tenant: Tenant, today: date) -> None:
    """Own a missing, refused or expired termo on each live SERPRO connection in use."""
    if not tenant_has(tenant.pk, "federal_checks"):
        return
    connections = ProviderConnection.objects.filter(
        tenant=tenant,
        provider="serpro",
        environment=settings.SERPRO_ENVIRONMENT,
        status="ativa",
    ).order_by("pk")
    for connection in connections:
        _own_unsettled_submission(connection)
        in_use = ClientProviderLink.objects.filter(
            connection=connection, status="ativa", disablement_state="ativa"
        ).exists()
        if not in_use or termo_in_force(connection.pk, today):
            continue
        head = termo_head(connection.pk)
        own_firm_condition(connection, PROC_MISSING, [str(head.pk)] if head else [])

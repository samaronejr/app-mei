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
from apps.integrations.authority import (
    OPERATION_REQUIREMENTS,
    Problem,
    raise_problem,
    record_authority_denial,
)
from apps.integrations.call_contracts import Claim
from apps.integrations.call_models import CallState, FirmProviderCall, ProviderCall
from apps.integrations.models import (
    AUTHORITY_CATALOG,
    CODE_NOT_REQUIRED,
    ClientProviderLink,
    FirmAuthorization,
    FirmAuthorizationStatus,
    ProviderConnection,
)
from apps.integrations.serpro.denials import (
    FIRM_DENIAL_CODES,
    PLATFORM_CAUSES,
    DenialCause,
    denial_cause,
)
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.termo_state import termo_head, termo_in_force
from apps.tenants.models import Tenant

AUTH_FAILED: Final = "autenticacao_serpro_recusada"
PROC_MISSING: Final = "autorizacao_procurador_ausente"
# A firm block that refused a call and has cleared since it was recorded.
CLEARED_PREFIX: Final = "encerrado-"
# Admission refusals: the client's own absence, or one with no recorded cause.
CLIENT_ABSENT: Final = "autorizacao_ausente"
ADMISSION_REFUSED: Final = "admissao_recusada"
# Firm blocks, named when admission refuses: the termo, or the 403 behind the issue.
TERMO_NOT_IN_FORCE: Final = "termo_nao_vigente"
BLOCK_PREFIX: Final = "bloqueio-"
# Identities of a recorded firm block: the issue, or the termo at a version.
ISSUE_PREFIX: Final = "pendencia-"
TERMO_PREFIX: Final = f"{TERMO_NOT_IN_FORCE}-v"
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


def own_denial(call: ProviderCall) -> DenialCause:
    """Route one billed SERPRO 403 to the owner its reason code names (A6).

    Idempotent: the protocol's post-call hook and the consumer settling the call
    both run it. Only the client-procuracao codes touch a client's authority (and
    only for a service that needs one). A firm-termo or platform code opens the
    firm issue, which already refuses that firm's calls at admission, so such a
    billed call is not repeated. A provider failure or an unknown code changes
    nothing here, and nothing re-sends a billed denial.
    """
    cause = denial_cause(call.provider_code)
    service = OPERATION_REQUIREMENTS[call.operation].service
    if (
        cause == DenialCause.PROCURACAO_CLIENTE
        and service is not None
        and AUTHORITY_CATALOG[service][1] != CODE_NOT_REQUIRED
    ):
        record_authority_denial(
            call.client, call.operation, source_call_ref=str(call.pk), http_status=403
        )
    elif cause == DenialCause.TERMO_ESCRITORIO or cause in PLATFORM_CAUSES:
        own_firm_condition(call.link.connection, PROC_MISSING, [str(call.pk)])
    return cause


def _cited(refs: list[str]) -> list[UUID]:
    cited = []
    for ref in refs:
        try:
            cited.append(UUID(ref))
        except ValueError:
            continue
    return cited


def firm_block(connection: ProviderConnection) -> str:
    """Name the firm's block by its identity, or '' when there is none.

    The open firm issue by its id (`pendencia-<id>`), else the termo not in force
    at this connection version (`termo_nao_vigente-v<version>`): a later block of
    the same kind is a different blocker, never this one (AXRM-045).
    """
    issue = (
        FirmIntegrationIssue.objects.filter(
            connection_id=connection.pk, reason=PROC_MISSING, state__in=ACTIVE_STATES
        )
        .order_by("created_at", "pk")
        .first()
    )
    if issue is not None:
        return f"{ISSUE_PREFIX}{issue.pk}"
    if not termo_in_force(connection.pk, timezone.localdate()):
        return f"{TERMO_PREFIX}{connection.version}"
    return ""


def issue_denial_code(issue_id: str) -> str:
    """Return the firm-side 403 code a firm issue cites, or '' if none."""
    issue = FirmIntegrationIssue.objects.filter(pk__in=_cited([issue_id])).first()
    if issue is None:
        return ""
    return (
        ProviderCall.objects.filter(
            pk__in=_cited(issue.evidence_refs), provider_code__in=FIRM_DENIAL_CODES
        )
        .order_by("pk")
        .values_list("provider_code", flat=True)
        .first()
        or ""
    )


def block_holds(reason: str, connection: ProviderConnection) -> bool:
    """Whether the specific blocker a refusal recorded still holds.

    That issue still open, or the termo still not in force at that same version.
    A code recorded without an identity (an older one) never holds unless proven.
    """
    if reason.startswith(ISSUE_PREFIX):
        cited = _cited([reason.removeprefix(ISSUE_PREFIX)])
        return FirmIntegrationIssue.objects.filter(
            pk__in=cited, state__in=ACTIVE_STATES
        ).exists()
    if reason.startswith(TERMO_PREFIX):
        version = reason.removeprefix(TERMO_PREFIX)
        return (
            version.isdigit()
            and connection.version == int(version)
            and not termo_in_force(connection.pk, timezone.localdate())
        )
    return False


def firm_reason(reason: str) -> bool:
    """Whether a recorded refusal reason names a firm block (with or without id)."""
    return reason in {PROC_MISSING, TERMO_NOT_IN_FORCE} or reason.startswith(
        (ISSUE_PREFIX, TERMO_PREFIX, BLOCK_PREFIX)
    )


def refusal_cause(reason: str, connection: ProviderConnection) -> str:
    """Classify an admission refusal by the cause recorded when it happened.

    '' only for the client's own absent authority. A firm refusal keeps the
    blocker it named while that one holds; once it has cleared it is
    `encerrado-<blocker>`. A refusal with no firm or client cause is never the
    client's (AXRM-045).
    """
    if reason == CLIENT_ABSENT:
        return ""
    if firm_reason(reason):
        return (
            reason if block_holds(reason, connection) else f"{CLEARED_PREFIX}{reason}"
        )
    return ADMISSION_REFUSED


def own_client_result(ticket: Claim) -> None:
    """Only finalized SERPRO conditions open work, never arbitrary provider prose."""
    with tenant_context(ticket.tenant_id):
        call = ProviderCall.objects.select_related("client").get(
            pk=ticket.call_id, client_id=ticket.client_id
        )
        if call.provider != "serpro":
            return
        if call.state == CallState.DENIED:
            own_denial(call)
        elif call.http_status == 401 and call.attempts == 2:  # noqa: PLR2004
            raise_problem(
                call.client,
                Problem(
                    AUTH_FAILED,
                    call.pk,
                    review_on=timezone.localdate(),
                    service=call.operation,
                ),
            )


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

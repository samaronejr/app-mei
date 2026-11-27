"""Database-only admission locks and uncertainty work for provider calls."""

from typing import Final
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.core.flags import tenant_row_has
from apps.core.tenancy import tenant_context
from apps.integrations.authority import client_owner
from apps.integrations.budget_alerts import refused
from apps.integrations.budget_pricing import BudgetDeniedError
from apps.integrations.budget_settlement import quarantine, release
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    AdmissionPolicy,
    Operation,
)
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.models import (
    ACTIVE_ISSUE_STATES,
    ClientProviderLink,
    IntegrationIssue,
    IssueReason,
    ProviderConnection,
)
from apps.tenants.models import Tenant


def current_epoch() -> int:
    """Read the platform epoch through the runtime-only initialization function."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT integrations_current_provider_epoch()")
        return int(cursor.fetchone()[0])


def lock_controls(tenant_id: UUID, link_id: UUID) -> tuple[Tenant, ClientProviderLink]:
    """Use one lock order for reservation, claims and dispatch-start commits."""
    # PLATFORM_QUERY_OK: lock exactly the explicit tenant's feature entitlement.
    # Existing link writers append audit rows whose tenant FK needs KEY SHARE.
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id)
    connection_id = ClientProviderLink.objects.values_list(
        "connection_id", flat=True
    ).get(pk=link_id)
    provider = ProviderConnection.objects.select_for_update().get(pk=connection_id)
    link = ClientProviderLink.objects.select_for_update().get(pk=link_id)
    link.connection = provider
    return tenant, link


def admit(call: ProviderCall, tenant: Tenant, operation: Operation) -> None:
    """Recheck closed defaults and mandatory live policy under the control locks."""
    link = call.link
    provider = link.connection
    if (
        settings.PROVIDERS_PAUSED
        or getattr(settings, f"{call.provider.upper()}_PAUSED")
        or not tenant.is_active
        or not tenant_row_has(tenant, operation.feature)
        or provider.status != "ativa"
        or link.status != "ativa"
        or link.disablement_state != "ativa"
        or call.environment != getattr(settings, f"{call.provider.upper()}_ENVIRONMENT")
        or (call.environment == "producao" and not settings.PROVIDER_REAL_DATA_APPROVED)
    ):
        raise AdmissionDeniedError(CallState.PAUSED)
    if (
        provider.provider != operation.provider
        or call.operation_revision != operation.revision
        or call.connection_version != provider.version
        or call.link_version != link.version
        or call.credential_generation != link.credential_generation
        or (
            operation.feature == "document_capture"
            and link.credential_state not in {"ativa", "vencendo"}
        )
    ):
        reason = _moved_cause(operation.policy, call)
        raise AdmissionDeniedError(CallState.AUTHORITY, reason=reason)
    if not operation.policy.authorize(call):
        raise AdmissionDeniedError(CallState.AUTHORITY)


# The states `record_refusal` leaves on a refused call.
RECORDED_REFUSALS: Final = (CallState.AUTHORITY, CallState.PAUSED, CallState.QUOTA)


def refusal_recorded(tenant_id: UUID, call_id: UUID) -> bool:
    """Whether a refusal was recorded and committed on this call (AXRM-045)."""
    with tenant_context(tenant_id):
        return ProviderCall.objects.filter(
            pk=call_id, state__in=RECORDED_REFUSALS
        ).exists()


def record_refusal(call: ProviderCall, error: AdmissionDeniedError) -> None:
    """Record an admission refusal on the call and release its reservation.

    One handling for claim and dispatch: a refused call is never left reserved,
    and the cause the refusal names stays with it (AXRM-045). A pause never ends
    a call that already sent a request: it goes back to retry, still owned by
    its sweep or settlement, with only its next attempt's reservation released.
    """
    release(call)
    if (
        call.attempts > 0
        and str(error) == CallState.PAUSED
        and not isinstance(error, BudgetDeniedError)
    ):
        call.state = CallState.RETRY
    elif isinstance(error, BudgetDeniedError):
        call.state = CallState.QUOTA
        call.provider_code = error.reason
        refused(call)
    else:
        call.state = (
            CallState.PAUSED if str(error) == CallState.PAUSED else CallState.AUTHORITY
        )
        # The cause as it stood when refused, durable with the refusal.
        call.provider_code = error.reason
    call.version += 1
    call.save()


def end_held(
    tenant_id: UUID, client_id: UUID, request_key: str, reason: str
) -> ProviderCall | None:
    """End a call already made that the live controls no longer admit.

    A held call (reserved, or a sent one waiting to retry) is recorded refused,
    so it is terminal and owned, never stranded; nothing is sent (AXRM-045).
    """
    with tenant_context(tenant_id), transaction.atomic():
        call = (
            ProviderCall.objects.select_for_update()
            .filter(client_id=client_id, request_key=request_key)
            .first()
        )
        if call is not None and call.state in {CallState.RESERVED, CallState.RETRY}:
            record_refusal(
                call, AdmissionDeniedError(CallState.AUTHORITY, reason=reason)
            )
        return call


def _moved_cause(policy: AdmissionPolicy, call: ProviderCall) -> str:
    """Name the blocker a call meets now, once its versions moved after reservation.

    A moved version is no cause in itself, and the call is refused with its
    reservation released whatever the re-read does: it may only choose the cause.
    A wait, a deferral or an error names none; its writes are rolled back.
    """
    try:
        with transaction.atomic():
            policy.authority(call)
    except AdmissionDeniedError as refused:
        return refused.reason if str(refused) == CallState.AUTHORITY else ""
    except Exception:  # noqa: BLE001 - the refusal stands whatever the re-read does
        return ""
    return ""


def uncertain(call: ProviderCall) -> None:
    """Quarantine an ambiguous attempt; never release its maximum exposure."""
    quarantine(call)
    call.state = CallState.UNCERTAIN
    call.version += 1
    call.updated_at = timezone.now()
    call.save()
    _own_uncertainty(call)


def _own_uncertainty(call: ProviderCall) -> None:
    """Converge on AXRM-008's one active issue per client, reason and operation.

    An issue already being handled (`em_tratamento`) absorbs the call, and every
    uncertain call stays discoverable in `evidence_refs`. Without any owner the call
    stays quarantined but no unowned issue is opened (AXRM-008's rule).
    """
    active = IntegrationIssue.objects.select_for_update().filter(
        client_id=call.client_id,
        reason=IssueReason.CHAMADA_PROVEDOR_INCERTA,
        service=call.operation,
        state__in=ACTIVE_ISSUE_STATES,
    )
    issue = active.first()
    if issue is None:
        owner = client_owner(call.client)
        if owner is None:
            return
        try:
            with transaction.atomic():
                issue = IntegrationIssue.objects.create(
                    tenant_id=call.tenant_id,
                    client_id=call.client_id,
                    reason=IssueReason.CHAMADA_PROVEDOR_INCERTA,
                    service=call.operation,
                    link_id=call.link_id,
                    owner=owner,
                    review_on=timezone.localdate(),
                    evidence_refs=[str(call.pk)],
                )
        except IntegrityError:
            # A concurrent opener committed the active row first: join it.
            issue = active.get()
        else:
            record_event(
                action="integrations.issue_opened",
                tenant_id=call.tenant_id,
                obj=ObjectRef("IntegrationIssue", str(issue.pk)),
                metadata={"reason": issue.reason, "service": issue.service},
            )
            return
    if str(call.pk) not in issue.evidence_refs:
        issue.evidence_refs = [*issue.evidence_refs, str(call.pk)]
        issue.version += 1
        issue.updated_at = timezone.now()
        issue.save(update_fields=["evidence_refs", "version", "updated_at"])

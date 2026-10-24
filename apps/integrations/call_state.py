"""Database-only admission locks and uncertainty work for provider calls."""

from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.core.flags import tenant_row_has
from apps.integrations.authority import client_owner
from apps.integrations.call_contracts import AdmissionDeniedError, Operation
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
        or not operation.policy.authorize(call)
    ):
        raise AdmissionDeniedError(CallState.AUTHORITY)


def uncertain(call: ProviderCall) -> None:
    """Quarantine an ambiguous attempt; never release its maximum exposure."""
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

"""Owned, firm-only quota work, including provider-side capture overage warnings."""

from typing import Final
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.cycles.models import CycleState, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import owner_for
from apps.cycles.services.exceptions import ExceptionReason, open_exception
from apps.integrations.authority import client_owner
from apps.integrations.call_models import ProviderCall
from apps.integrations.models import (
    ACTIVE_ISSUE_STATES,
    ClientProviderLink,
    IntegrationIssue,
    IssueReason,
)

NEAR: Final = IssueReason.COTA_PROVEDOR_PROXIMA
EXHAUSTED: Final = IssueReason.COTA_PROVEDOR_ESGOTADA
REASONS: Final = {
    NEAR: ExceptionReason(
        "Revise o consumo e a cota antes de solicitar novas consultas. "
        "No Focus, limite do Axorim - nao e um teto do provedor.",
        blocks_completion=False,
        label="cota do provedor próxima",
    ),
    EXHAUSTED: ExceptionReason(
        "Pause novas consultas e revise a cota. No Focus, solicite à operação "
        "focus_company_disable e confirme a desativação no provedor; "
        "pausar chamadas locais não interrompe a captura no provedor.",
        label="cota do provedor esgotada",
    ),
}


def _own_issue(
    link: ClientProviderLink, reason: str, subject: UUID, service: str
) -> None:
    """Converge on AXRM-008's one active issue per client, reason and service.

    Same pattern as call-uncertainty work: lock the active row, otherwise create it
    in a savepoint and join a concurrent opener's row; every subject stays listed.
    Without any owner no unowned issue is opened (AXRM-008's rule).
    """
    active = IntegrationIssue.objects.select_for_update().filter(
        client_id=link.client_id,
        reason=reason,
        service=service,
        state__in=ACTIVE_ISSUE_STATES,
    )
    issue = active.first()
    if issue is None:
        owner = client_owner(link.client)
        if owner is None:
            return
        try:
            with transaction.atomic():
                issue = IntegrationIssue.objects.create(
                    tenant_id=link.tenant_id,
                    client_id=link.client_id,
                    reason=reason,
                    service=service,
                    link=link,
                    owner=owner,
                    review_on=timezone.localdate(),
                    evidence_refs=[str(subject)],
                )
        except IntegrityError:
            # A concurrent opener committed the active row first: join it.
            issue = active.get()
        else:
            record_event(
                action="integrations.issue_opened",
                tenant_id=link.tenant_id,
                obj=ObjectRef("IntegrationIssue", str(issue.pk)),
                metadata={"reason": issue.reason, "service": issue.service},
            )
            return
    if str(subject) not in issue.evidence_refs:
        issue.evidence_refs = [*issue.evidence_refs, str(subject)]
        issue.version += 1
        issue.updated_at = timezone.now()
        issue.save(update_fields=["evidence_refs", "version", "updated_at"])


def open_issue(
    link: ClientProviderLink, reason: str, subject: UUID, service: str = ""
) -> None:
    """Own the quota problem and materialize it in the client's current cycles.

    `service` is the A6 operation for call-based work and blank for the binding's
    own (Focus document overage) problem, which `link` names.
    """
    _own_issue(link, reason, subject, service)
    for cycle in (
        ServiceCycle.objects.select_for_update(of=("self",))
        .filter(
            client_id=link.client_id, period_start=timezone.now().date().replace(day=1)
        )
        .exclude(
            state__in=(
                CycleState.COMPLETED,
                CycleState.WITH_PENDING,
                CycleState.CANCELLED,
            )
        )
        .select_related("owner", "engagement__responsible")
    ):
        responsible = owner_for(cycle)
        if (
            responsible is not None
            and not CycleException.objects.filter(
                cycle=cycle, reason_code=reason, subject_ref=subject
            ).exists()
        ):
            open_exception(
                reason,
                subject,
                actor=None,
                cycle_id=cycle.pk,
                expected_version=cycle.version,
                owner=responsible,
                review_on=timezone.localdate(),
                evidence_refs=(subject,),
            )


def refused(call: ProviderCall) -> None:
    """Keep a refused reservation visible even though no I/O took place."""
    open_issue(call.link, EXHAUSTED, call.pk, service=call.operation)

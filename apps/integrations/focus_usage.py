"""Meter captured documents once per calendar month, never once per HTTP request."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from django.conf import settings

from apps.core.tenancy import tenant_context
from apps.integrations.budget import lock_budget
from apps.integrations.budget_alerts import EXHAUSTED, NEAR, open_issue
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.budget_pricing import BudgetDeniedError, microbrl
from apps.integrations.models import ClientProviderLink
from apps.tenants.models import UsageKind, UsageMetric, UsageRecord


@dataclass(frozen=True, slots=True)
class CapturedRevision:
    """The register's immutable identity and observation time, not a call/page count."""

    tenant_id: UUID
    client_id: UUID
    document_id: UUID
    revision_id: UUID
    observed_at: datetime


def record_captured_revision(link_id: UUID, revision: CapturedRevision) -> bool:
    """Count a captured document once in its observed UTC month under a budget lock."""
    month = revision.observed_at.astimezone(UTC).date().replace(day=1)
    with tenant_context(revision.tenant_id):
        link = ClientProviderLink.objects.select_related("connection").get(
            pk=link_id, client_id=revision.client_id, connection__provider="focus"
        )
        lock_budget()
        budget = (
            ProviderBudget.objects.select_for_update()
            .filter(provider="focus", window_start=month)
            .first()
        )
        if budget is None:
            reason = "cota_ausente"
            raise BudgetDeniedError(reason)
        _, created = UsageRecord.objects.get_or_create(
            metric=UsageMetric.FOCUS_DOCUMENTOS,
            source_event=uuid5(revision.document_id, f"focus-document:{month}"),
            defaults={
                "tenant_id": revision.tenant_id,
                "period": month,
                "quantity": 1,
                "kind": UsageKind.ESTIMATED,
            },
        )
        if created:
            count = UsageRecord.objects.filter(
                metric=UsageMetric.FOCUS_DOCUMENTOS, period=month
            ).count()
            overage = max(0, count - budget.included_documents)
            if overage:
                budget.uncertain_exposure = overage * microbrl(
                    settings.USAGE_UNIT_PRICES_BRL.get(UsageMetric.FOCUS_DOCUMENTOS)
                )
                budget.version += 1
                budget.save()
            # Do not pretend the local cap limits provider-side capture. Retain
            # evidence even when it proves the provider has exceeded our ceiling.
            if budget.uncertain_exposure >= budget.cap_microbrl:
                open_issue(link, EXHAUSTED, budget.pk)
            elif budget.uncertain_exposure * 5 >= budget.cap_microbrl * 4:
                open_issue(link, NEAR, budget.pk)
        return created

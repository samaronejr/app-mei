"""Coverage items use T17's answer, never another freshness calculation."""

from collections.abc import Sequence
from datetime import date, datetime

from django.urls import reverse

from apps.accounts.models import User
from apps.briefing.rules import PriorityFacts, priority_factors
from apps.briefing.types import BriefingItem
from apps.clients.models import ClientCompany, Engagement
from apps.obligations.coverage import CoverageState, coverage_for_clients


def coverage_items(
    _actor: User,
    clients: Sequence[ClientCompany],
    period: date,
    *,
    now: datetime,
) -> list[BriefingItem]:
    """Surface only states the authoritative coverage reader calls stale or failed."""
    items: list[BriefingItem] = []
    answers_by_client = coverage_for_clients(clients, period, now=now)
    needs_review = {
        answer.engagement_id
        for answers in answers_by_client.values()
        for answer in answers
        if answer.state in {CoverageState.STALE, CoverageState.REFRESH_FAILED}
    }
    if not needs_review:
        return items
    engagements = {
        row.pk: row
        for row in Engagement.objects.filter(
            pk__in=needs_review,
            client_id__in=[client.pk for client in clients],
        ).select_related("responsible")
    }
    for client in clients:
        for answer in answers_by_client[client.pk]:
            if answer.state not in {
                CoverageState.STALE,
                CoverageState.REFRESH_FAILED,
            }:
                continue
            engagement = engagements[answer.engagement_id]
            items.append(
                BriefingItem(
                    kind="coverage",
                    subject_ref=engagement.pk,
                    client_id=client.pk,
                    period=period,
                    owner_id=engagement.responsible_id,
                    owner_name=(
                        engagement.responsible.email
                        if engagement.responsible is not None
                        else ""
                    ),
                    due_on=None,
                    factors=priority_factors(
                        PriorityFacts(stale_coverage=True), now=now
                    ),
                    source_observed_at=answer.last_success,
                    next_action="Revisar a cobertura e suas fontes",
                    evidence_refs=(str(answer.head_id),) if answer.head_id else (),
                    fact_version=f"{answer.head_id}:{answer.state}:{engagement.version}",
                    client_name=client.legal_name,
                    title="Cobertura exige revisão",
                    url=(
                        f"{reverse('client-detail', args=[client.pk])}"
                        f"?periodo={period:%Y-%m}"
                    ),
                    service_kind=answer.service_kind,
                    work_state=answer.state,
                    freshness=answer.state,
                )
            )
    return items

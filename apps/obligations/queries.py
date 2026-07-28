"""The four work queues an accountant opens the product to look at.

Every one of them answers the same shape of question — *what needs attention, in the
book of business this account is responsible for* — so they share one definition of
that book (`visible_clients`) and differ only in what "needs attention" means.

Two decisions are worth stating outright.

**Overdue is decided by the date, not by the status column.** There is an `overdue`
status and a nightly sweep that sets it, but a queue that trusts the label reports an
empty list on any morning the scheduler did not run. Silence is this product's worst
failure mode, so the queue compares `resolved_due_date` against today and treats
anything unsettled as late.

**The threshold queue aggregates in the database.** `threshold_status` is a
per-client function that reads revenue and three fiscal parameters, so calling it in
a loop is one-plus-three queries per client. Here the revenue is summed once with a
grouped annotation and the parameters are resolved once through a shared
`ParameterCache`, then handed to the same arithmetic — one code path for the four
bands, and a cost that does not move when the portfolio grows.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from django.db.models import Q, QuerySet, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.authz.portfolio import visible_clients
from apps.authz.services import Actor
from apps.clients.models import ClientCompany, OnboardingItem, OnboardingStatus
from apps.obligations.models import Obligation, ObligationStatus
from apps.obligations.parameters import ParameterCache
from apps.obligations.threshold import ThresholdBand, ThresholdStatus, threshold_status

DUE_SOON_HORIZON_DAYS: Final = 7

# An obligation in either of these states needs no further work, whatever its date.
SETTLED: Final = (ObligationStatus.PAID, ObligationStatus.WAIVED)

# The bands that put a client on the threshold queue. `ok` is the only one that does
# not: the two exceeded bands are a year apart in consequence and both need acting on.
ATTENTION_BANDS: Final = frozenset(
    {
        ThresholdBand.WARNING,
        ThresholdBand.EXCEEDED_WITHIN_TOLERANCE,
        ThresholdBand.EXCEEDED_OVER_TOLERANCE,
    },
)


@dataclass(frozen=True, slots=True)
class ThresholdRow:
    """One client on the threshold queue, with the band it matched on."""

    client: ClientCompany
    status: ThresholdStatus


def _today(today: date | None) -> date:
    """Resolve "today" in Brasília, since that is the day a deadline is measured in."""
    return today if today is not None else timezone.localdate()


def _unsettled(user: Actor, tenant_id: UUID | None) -> QuerySet[Obligation]:
    return (
        Obligation.objects.filter(
            client__in=visible_clients(user, tenant_id=tenant_id),
        )
        .exclude(status__in=SETTLED)
        # Both are rendered on every row of the queue. Without this the template
        # issues two queries per row and the budget test is what notices.
        .select_related("client", "obligation_type")
    )


def due_soon(
    user: Actor,
    *,
    today: date | None = None,
    horizon_days: int = DUE_SOON_HORIZON_DAYS,
    tenant_id: UUID | None = None,
) -> QuerySet[Obligation]:
    """Obligations resolving within the next `horizon_days`, today included.

    The horizon is inclusive at both ends: something due today is the most urgent
    thing on the list, and excluding the final day would hide every obligation on the
    morning it became due.
    """
    reference = _today(today)
    horizon = reference + timedelta(days=horizon_days)
    return (
        _unsettled(user, tenant_id)
        .filter(
            resolved_due_date__gte=reference,
            resolved_due_date__lte=horizon,
        )
        .order_by("resolved_due_date", "client__legal_name")
    )


def overdue(
    user: Actor,
    *,
    today: date | None = None,
    tenant_id: UUID | None = None,
) -> QuerySet[Obligation]:
    """Obligations whose resolved due date has passed and which are still unsettled."""
    return (
        _unsettled(user, tenant_id)
        .filter(resolved_due_date__lt=_today(today))
        .order_by("resolved_due_date", "client__legal_name")
    )


def onboarding_blocked(
    user: Actor,
    *,
    tenant_id: UUID | None = None,
) -> QuerySet[OnboardingItem]:
    """Checklist items a client's onboarding is stuck on."""
    return (
        OnboardingItem.objects.filter(
            client__in=visible_clients(user, tenant_id=tenant_id),
            status=OnboardingStatus.BLOCKED,
        )
        .select_related("client")
        .order_by("client__legal_name", "position", "key")
    )


def threshold_warning(
    user: Actor,
    *,
    year: int | None = None,
    today: date | None = None,
    tenant_id: UUID | None = None,
) -> tuple[ThresholdRow, ...]:
    """Clients at or past 80% of the ceiling that applies to them.

    Returns a tuple rather than a queryset because the band is not a column: it comes
    from arithmetic over a summed total, a proportional ceiling and a tolerance rate,
    all of which are effective-dated data. Materialising is therefore unavoidable —
    what is avoided is doing it one client at a time. An empty portfolio yields an
    empty tuple, never `None`.
    """
    reference_year = year if year is not None else _today(today).year
    within_year = Q(
        monthly_revenues__competence_month__year=reference_year,
    )
    clients = (
        visible_clients(user, tenant_id=tenant_id)
        .annotate(
            billed=Coalesce(
                Sum("monthly_revenues__gross_amount", filter=within_year),
                Decimal("0.00"),
            ),
        )
        .order_by("legal_name")
    )

    parameters = ParameterCache()
    rows = (
        ThresholdRow(
            client=client,
            status=threshold_status(
                client,
                reference_year,
                accumulated=client.billed,
                parameters=parameters,
            ),
        )
        for client in clients
    )
    return tuple(row for row in rows if row.status.band in ATTENTION_BANDS)


__all__ = [
    "ATTENTION_BANDS",
    "DUE_SOON_HORIZON_DAYS",
    "SETTLED",
    "ThresholdRow",
    "due_soon",
    "onboarding_blocked",
    "overdue",
    "threshold_warning",
]

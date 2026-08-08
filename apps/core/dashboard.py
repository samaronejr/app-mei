"""The accountant's dashboard: the first screen a human being sees in this product.

It is also the first place the isolation model becomes visible, which is why every
number on it is derived from the same scoped helpers the queues use rather than from
a query written here. There is exactly one definition of "this account's portfolio" —
`apps.authz.portfolio.visible_clients` — and the portfolio counts, the queue counts,
the threshold list and the calendar's client selector all narrow from it.

The twelve-month DAS calendar reads the `Obligation` rows the generator produced; it
does **not** recompute the dates for display. Recomputing would draw a plausible
calendar on a morning the scheduler never ran, which is the failure this product is
built to make loud rather than quiet — so when there are no rows the strip says so.
"""

from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from typing import Final
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.db.models import Count, QuerySet
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import visible_clients
from apps.authz.services import require_can
from apps.clients.models import ClientCompany, ClientStatus
from apps.obligations.models import Obligation
from apps.obligations.queries import threshold_warning
from apps.obligations.views import queue_counts
from apps.tenants.models import Tenant

CALENDAR_MONTHS: Final = 12
CLIENT_PARAM: Final = "cliente"
DAS_CODE: Final = "DAS"


@dataclass(frozen=True, slots=True)
class CalendarCell:
    """One month of the DAS strip."""

    competence: date
    nominal: date
    resolved: date

    @property
    def was_rolled(self) -> bool:
        """Report whether the deadline moved off its nominal day.

        Shown because an accountant planning a payment run needs to know the 20th
        became the 22nd, and because a strip where nothing ever rolls is the visible
        symptom of a business-day calendar that stopped working.
        """
        return self.nominal != self.resolved


def _tenant(request: AuthenticatedRequest) -> Tenant:
    """Return the firm this request resolved to, or 404.

    The platform host resolves no tenant, and a dashboard with no portfolio to show
    is not found rather than empty — an empty one would imply the firm exists and has
    no clients.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    resolved: Tenant = tenant
    return resolved


def _portfolio_counts(clients: QuerySet[ClientCompany]) -> list[dict[str, object]]:
    """Count the portfolio by client status, keeping statuses with no clients at zero.

    Rendering only the statuses that happen to be present would make a firm with no
    suspended clients show a different set of tiles than one with a single suspended
    client, and a reader cannot tell a missing tile from a zero.
    """
    tallied = {
        row["status"]: row["total"]
        for row in clients.values("status").annotate(total=Count("pk"))
    }
    return [
        {"status": value, "label": label, "count": tallied.get(value, 0)}
        for value, label in ClientStatus.choices
    ]


def _selected_client(
    request: AuthenticatedRequest,
    clients: QuerySet[ClientCompany],
) -> ClientCompany | None:
    """Resolve the calendar's client from the query string, within the portfolio.

    Selected out of the scoped queryset rather than fetched by primary key, so naming
    another firm's client id yields the default rather than a row — and never turns a
    blank calendar into a confirmation that the id was real.
    """
    requested = request.GET.get(CLIENT_PARAM, "").strip()
    ordered = clients.order_by("legal_name", "pk")
    if requested:
        try:
            chosen: ClientCompany | None = ordered.filter(pk=UUID(requested)).first()
        except ValueError:
            # Not a UUID at all. A typed or truncated URL, not something to 500 over.
            chosen = None
        if chosen is not None:
            return chosen
    first: ClientCompany | None = ordered.first()
    return first


def das_calendar(client: ClientCompany | None) -> list[CalendarCell]:
    """Read the next twelve DAS deadlines this client already has rows for.

    Public because the client detail screen draws the same twelve months from the same
    rows. Two readers would be two chances to disagree about which deadlines exist, and
    a calendar that disagrees with itself is worse than no calendar — this is the one
    artefact an accountant plans a payment run from.
    """
    if client is None:
        return []
    upcoming = (
        Obligation.objects.filter(
            client=client,
            obligation_type__code=DAS_CODE,
        )
        .order_by("competence_month")
        .values_list("competence_month", "nominal_due_date", "resolved_due_date")[
            :CALENDAR_MONTHS
        ]
    )
    return [
        CalendarCell(competence=competence, nominal=nominal, resolved=resolved)
        for competence, nominal, resolved in upcoming
    ]


@require_http_methods(["GET"])
@login_required
@require_can("clients.view_assigned")
def dashboard_view(request: AuthenticatedRequest) -> HttpResponse:
    """Render the firm's portfolio, its work queues and one client's DAS calendar."""
    tenant = _tenant(request)
    tenant_id = UUID(str(tenant.pk))
    clients = visible_clients(request.user, tenant_id=tenant_id)
    selected = _selected_client(request, clients)

    return render(
        request,
        "core/dashboard.html",
        {
            "portfolio": _portfolio_counts(clients),
            "portfolio_total": clients.count(),
            "counts": queue_counts(request),
            "calendar": das_calendar(selected),
            "calendar_client": selected,
            "clients": clients.order_by("legal_name", "pk"),
            "threshold_rows": threshold_warning(request.user, tenant_id=tenant_id),
            "page_title": tenant.name,
        },
        status=HTTPStatus.OK,
    )


__all__ = ["CALENDAR_MONTHS", "CalendarCell", "das_calendar", "dashboard_view"]

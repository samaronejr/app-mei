"""The shared, authorized exception drawer projection for both staff surfaces."""

from uuid import UUID

from django.db.models import Q, QuerySet
from django.http import Http404

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import PortfolioScope, portfolio_scope
from apps.authz.services import can
from apps.clients.models import ClientAssignment
from apps.cycles.capabilities import CYCLES_REVIEW
from apps.cycles.exception_evidence import evidence_destinations, exception_location
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ExceptionState
from apps.cycles.services.cycle import tenant_id
from apps.cycles.services.exceptions import (
    ACTIVE_STATES,
    EXCEPTION_TRANSITIONS,
    is_waivable,
    reason_for,
)
from apps.tenants.models import Membership

EXCEPTION_ACTION_LABELS = {
    ExceptionState.HANDLING: "Começar a tratar",
    ExceptionState.RESOLVED: "Marcar como resolvida",
    ExceptionState.RISK_ACCEPTED: "Aceitar como risco",
}


def _roster(client_id: UUID) -> list[Membership]:
    """Firm members an exception may be handed to (the service re-checks each)."""
    assigned = set(
        ClientAssignment.objects.filter(client_id=client_id).values_list(
            "user_id", flat=True
        )
    )
    members = (
        # PLATFORM_QUERY_OK: tenants_membership is a tenancy root; this firm's staff.
        Membership.objects.filter(
            tenant_id=tenant_id(), client__isnull=True, is_active=True
        )
        .select_related("user")
        .order_by("user__email")
    )
    return [
        member
        for member in members
        if member.user_id in assigned
        or portfolio_scope(member.user, tenant_id=tenant_id()) is PortfolioScope.ALL
    ]


def exception_rows(
    request: AuthenticatedRequest,
    rows: QuerySet[CycleException],
    origin: str,
    selected_exception: CycleException | None = None,
    error: str = "",
) -> list[dict[str, object]]:
    """Select only inside the caller's authorized client, period and cycle."""
    selected = selected_exception.pk if selected_exception is not None else None
    if selected is None and "pendencia" in request.GET:
        try:
            selected = UUID(request.GET["pendencia"])
        except ValueError as exc:
            raise Http404 from exc
    visible = list(
        rows.filter(Q(state__in=ACTIVE_STATES) | Q(pk=selected))
        .select_related("owner", "cycle__client", "cycle__engagement")
        .prefetch_related("internals")
        .order_by("due_on", "review_on", "created_at", "pk")
    )
    if selected is not None and not any(row.pk == selected for row in visible):
        raise Http404
    roster = _roster(visible[0].client_id) if visible else []
    may_review = can(request.user, CYCLES_REVIEW, tenant_id=tenant_id())
    evidence = evidence_destinations(visible, may_review)
    result = []
    for problem in visible:
        reason = reason_for(problem.reason_code)
        label = reason.label if reason and reason.label else "revisão necessária"
        result.append(
            {
                "exception": problem,
                "internal": next(iter(problem.internals.all())),
                "label": label,
                "title": f"Pendência: {label}",
                "is_active": problem.state in ACTIVE_STATES,
                "selected": problem.pk == selected,
                "error": error if problem.pk == selected else "",
                "dialog_id": f"exception-{problem.pk}",
                "trigger_id": f"exception-trigger-{problem.pk}",
                "url": exception_location(problem.cycle, problem.pk, origin),
                "close_url": exception_location(problem.cycle, None, origin),
                "evidence": evidence[problem.pk],
                "waivable": is_waivable(problem.reason_code),
                "actions": [
                    (target, EXCEPTION_ACTION_LABELS[target])
                    for target in (
                        ExceptionState.HANDLING,
                        ExceptionState.RESOLVED,
                        ExceptionState.RISK_ACCEPTED,
                    )
                    if target in EXCEPTION_TRANSITIONS.get(problem.state, frozenset())
                    and (
                        target != ExceptionState.RISK_ACCEPTED
                        or (may_review and is_waivable(problem.reason_code))
                    )
                ],
                "roster": roster,
            }
        )
    return result

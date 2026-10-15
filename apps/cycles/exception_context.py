"""The shared, authorized exception drawer projection for both staff surfaces."""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db.models import Q, QuerySet
from django.http import Http404

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import assignable_members
from apps.authz.services import can
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

if TYPE_CHECKING:
    from apps.cycles.display_reads import CycleDetailReads

EXCEPTION_ACTION_LABELS = {
    ExceptionState.HANDLING: "Começar a tratar",
    ExceptionState.RESOLVED: "Marcar como resolvida",
    ExceptionState.RISK_ACCEPTED: "Aceitar como risco",
}


def _roster(client_id: UUID) -> list[Membership]:
    """Firm members an exception may be handed to (the service re-checks each)."""
    return list(assignable_members(client_id, tenant_id=tenant_id()))


@dataclass(frozen=True, slots=True)
class ExceptionControls:
    """The reader's roster and review decision, not write authorization."""

    roster: list[Membership]
    may_review: bool


@dataclass(frozen=True, slots=True)
class ExceptionSelection:
    """Durable drawer selection and the selected row's conflict message."""

    selected: UUID | None
    error: str = ""


def exception_selection(
    request: AuthenticatedRequest,
    selected_exception: CycleException | None = None,
    error: str = "",
) -> ExceptionSelection:
    """Parse the selected identity once, refusing malformed UUIDs."""
    selected = selected_exception.pk if selected_exception is not None else None
    if selected is None and "pendencia" in request.GET:
        try:
            selected = UUID(request.GET["pendencia"])
        except ValueError as exc:
            raise Http404 from exc
    return ExceptionSelection(selected, error)


def _require_selection(
    visible: list[CycleException], selection: ExceptionSelection
) -> None:
    """Refuse a selected identity absent from either scoped read source."""
    if selection.selected is not None and not any(
        row.pk == selection.selected for row in visible
    ):
        raise Http404


def read_exceptions(
    rows: QuerySet[CycleException], selection: ExceptionSelection
) -> list[CycleException]:
    """Fetch the scoped active rows and any selected terminal row."""
    visible = list(
        rows.filter(Q(state__in=ACTIVE_STATES) | Q(pk=selection.selected))
        .select_related("owner", "cycle__client", "cycle__engagement")
        .prefetch_related("internals")
        .order_by("due_on", "review_on", "created_at", "pk")
    )
    _require_selection(visible, selection)
    return visible


def exception_rows(
    request: AuthenticatedRequest,
    rows: QuerySet[CycleException],
    origin: str,
    selected_exception: CycleException | None = None,
    error: str = "",
) -> list[dict[str, object]]:
    """Select only inside the caller's authorized client, period and cycle."""
    selection = exception_selection(request, selected_exception, error)
    visible = read_exceptions(rows, selection)
    roster = _roster(visible[0].client_id) if visible else []
    may_review = can(request.user, CYCLES_REVIEW, tenant_id=tenant_id())
    return present_exceptions(
        visible, selection, ExceptionControls(roster, may_review), origin
    )


def display_exception_rows(
    request: AuthenticatedRequest,
    reads: "CycleDetailReads",
    selected_exception: CycleException | None = None,
    error: str = "",
) -> list[dict[str, object]]:
    """Reuse the cycle display cut, retaining selected terminal rows and 404s."""
    selection = exception_selection(request, selected_exception, error)
    visible = [
        row
        for row in reads.exceptions
        if row.state in ACTIVE_STATES or row.pk == selection.selected
    ]
    _require_selection(visible, selection)
    roster = _roster(visible[0].client_id) if visible else []
    return present_exceptions(
        visible,
        selection,
        ExceptionControls(roster, CYCLES_REVIEW in reads.allowed),
        "ciclo",
    )


def present_exceptions(
    visible: list[CycleException],
    selection: ExceptionSelection,
    controls: ExceptionControls,
    origin: str,
) -> list[dict[str, object]]:
    """Render scoped rows using the originating reader's resolved controls."""
    selected, error = selection.selected, selection.error
    roster, may_review = controls.roster, controls.may_review
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

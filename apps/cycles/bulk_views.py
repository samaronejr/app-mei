"""Accessible rehearsal, exact confirmation and honest per-item progress."""

from collections import Counter
from datetime import date
from http import HTTPStatus
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import visible_clients
from apps.clients.models import Engagement
from apps.clients.views import require_tenant
from apps.cycles.bulk import (
    ACTIVE_OPERATIONS,
    UNFINISHED_ITEMS,
    BulkItemState,
    BulkKind,
    BulkOperation,
    BulkOperationState,
)
from apps.cycles.bulk_contracts import MAX_ITEMS, Selection
from apps.cycles.bulk_forms import BulkCommandForm, BulkConfigForm
from apps.cycles.errors import VersionConflict
from apps.cycles.forms import SERVICE_KIND_CHOICES
from apps.cycles.models import ActionProposal, ServiceCycle
from apps.cycles.services.bulk import (
    bulk_report_csv,
    cancel_bulk,
    checked_items,
    load_operation,
    preview_bulk,
    require_bulk,
    resume_bulk,
    run_bulk,
)
from apps.cycles.services.cycle import tenant_id
from apps.cycles.tasks import run_bulk_operation
from apps.delivery.services import remaining_quota

REASONS = {
    "client_inactive": "Cliente inativo.",
    "playbook_unpublished": "A versão não está publicada.",
    "engagement_outside_period": "O serviço não cobre esta competência.",
    "playbook_outside_scope": "O playbook não se aplica a este serviço ou regime.",
    "cycle_exists": "O ciclo desta competência já existe.",
    "capability_denied": "Você não tem a autorização necessária para este efeito.",
    "cycle_closed": "O ciclo está encerrado.",
    "no_drafts": "Nenhuma solicitação em rascunho.",
    "no_recipient": "Nenhum contato com acesso ao portal ou convite válido.",
    "different_playbook": "A versão pertence a outro playbook.",
    "not_newer_version": "Selecione uma versão posterior à atual.",
    "proposal_not_awaiting": "A proposta já recebeu uma decisão.",
    "proposal_expired": "A proposta expirou.",
    "individual_self_approval": (
        "A autoaprovação exige confirmação no cartão individual."
    ),
    "access_revoked": "O acesso foi revogado antes deste item.",
    "subject_changed": "O registro ou seus dados conferidos mudaram.",
    "execution_refused": (
        "O efeito foi recusado; nenhum efeito deste item foi aplicado."
    ),
    "proposal_invalidated": "A conferência dos efeitos invalidou esta proposta.",
    "cycle_opened": "Ciclo aberto.",
    "cycle_existing": "Ciclo já existente, sem repetir a criação.",
    "cycle_migrated": "Versão do ciclo atualizada.",
    "notices_queued": "Avisos na fila; isso não comprova envio ou entrega.",
    "approval_recorded": "Decisão registrada; nenhum efeito externo foi executado.",
    "cancelled": "Cancelado antes de executar.",
}
SERVICE_LABELS = dict(SERVICE_KIND_CHOICES)


def _subjects(
    request: AuthenticatedRequest, kind: BulkKind, period: date | None
) -> list[tuple[str, str]]:
    clients = visible_clients(request.user, tenant_id=tenant_id()).values("pk")
    if kind == BulkKind.OPEN_CYCLES:
        engagements = (
            Engagement.objects.filter(client_id__in=clients)
            .select_related("client")
            .order_by("client__legal_name", "pk")[:MAX_ITEMS]
        )
        return [
            (
                str(row.pk),
                f"{row.client.legal_name} · {SERVICE_LABELS[row.service_kind]}",
            )
            for row in engagements
        ]
    if kind == BulkKind.APPROVE_ITEMS:
        proposals = ActionProposal.objects.filter(client_id__in=clients)
        if period is not None:
            proposals = proposals.filter(cycle__period_start=period)
        return [
            (
                str(row.pk),
                (
                    f"{row.client.legal_name} · {row.get_action_kind_display()} "
                    f"· revisão {row.revision}"
                ),
            )
            for row in proposals.select_related("client").order_by(
                "client__legal_name", "pk"
            )[:MAX_ITEMS]
        ]
    cycles = ServiceCycle.objects.filter(client_id__in=clients)
    if period is not None:
        cycles = cycles.filter(period_start=period)
    return [
        (
            str(row.pk),
            (
                f"{row.client.legal_name} · {row.period_start:%m/%Y} "
                f"· {row.get_state_display()}"
            ),
        )
        for row in cycles.select_related("client").order_by("client__legal_name", "pk")[
            :MAX_ITEMS
        ]
    ]


@login_required
@require_tenant
@require_http_methods(["GET", "POST"])
def bulk_start(request: AuthenticatedRequest) -> HttpResponse:
    """Select named rows first; confirmation never re-runs these querysets."""
    require_bulk(request.user)
    data = request.POST if request.method == "POST" else request.GET.copy()
    if request.method == "GET" and not data:
        data["kind"] = BulkKind.OPEN_CYCLES
    form = BulkConfigForm(data)
    status = HTTPStatus.OK
    subjects: list[tuple[str, str]] = []
    selected = request.POST.getlist("subject_ids")
    if form.is_valid():
        kind = BulkKind(form.cleaned_data["kind"])
        period = form.cleaned_data["periodo"]
        subjects = _subjects(request, kind, period)
        if request.method == "POST":
            definition = form.cleaned_data["playbook_version_id"]
            try:
                identifiers = tuple(UUID(value) for value in selected)
                operation = preview_bulk(
                    request.user,
                    kind,
                    Selection(
                        identifiers,
                        period=period,
                        playbook_version_id=definition.pk if definition else None,
                    ),
                )
            except (ValidationError, ValueError) as error:
                form.add_error(
                    None, error if isinstance(error, ValidationError) else str(error)
                )
                status = HTTPStatus.UNPROCESSABLE_CONTENT
            else:
                return redirect("bulk-detail", pk=operation.pk)
    else:
        status = HTTPStatus.UNPROCESSABLE_CONTENT
    return render(
        request,
        "cycles/bulk_start.html",
        {"form": form, "subjects": subjects, "selected": selected, "limit": MAX_ITEMS},
        status=status,
    )


def _context(operation: BulkOperation) -> dict[str, object]:
    results = {row.subject_ref: row for row in operation.items.order_by("position")}
    counts = Counter(row.state for row in results.values())
    rows = [
        {
            "snapshot": item,
            "result": results.get(str(item.subject_ref)),
            "reason": REASONS.get(
                str(results[str(item.subject_ref)].result_json.get("code", ""))
                if str(item.subject_ref) in results
                else item.reason,
                "",
            ),
        }
        for item in checked_items(operation)
    ]
    failures = counts[BulkItemState.FAILED]
    label = operation.get_state_display()
    if (
        operation.state
        in {
            BulkOperationState.COMPLETED,
            BulkOperationState.COMPLETED_WITH_FAILURES,
        }
        and failures
    ):
        label = f"Concluído com falhas: {failures}"
    return {
        "operation": operation,
        "rows": rows,
        "eligible_count": sum(row.eligible for row in checked_items(operation)),
        "expired": operation.valid_until <= timezone.now(),
        "preview": operation.state == BulkOperationState.PREVIEW,
        "active": operation.state in ACTIVE_OPERATIONS,
        "resumable": operation.state
        in {BulkOperationState.QUEUED, BulkOperationState.RUNNING},
        "state_label": label,
        "done_count": counts[BulkItemState.DONE],
        "failure_count": failures,
        "remaining_count": sum(counts[state] for state in UNFINISHED_ITEMS),
        "finished_count": sum(
            number for state, number in counts.items() if state not in UNFINISHED_ITEMS
        ),
        "total_count": len(rows),
    }


def _dispatch(operation: BulkOperation) -> None:
    """Dispatch after commit; leave durable work for the recovery sweep."""
    transaction.on_commit(
        lambda: run_bulk_operation.delay(str(operation.tenant_id), str(operation.pk))
    )


@login_required
@require_tenant
@require_http_methods(["GET", "POST"])
def bulk_detail(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Carry revision and digest on every confirmation, and retain validation input."""
    operation = load_operation(request.user, pk)
    error = ""
    status = HTTPStatus.OK
    if request.method == "POST":
        form = BulkCommandForm(request.POST)
        if not form.is_valid():
            error = "Revise os dados informados."
            status = HTTPStatus.UNPROCESSABLE_CONTENT
        else:
            data = form.cleaned_data
            try:
                if data["action"] == "confirm":
                    operation = run_bulk(
                        request.user,
                        pk,
                        expectation=form.expectation(),
                        confirm_count=int(data["confirm_count"]),
                        exclusions=tuple(request.POST.getlist("exclusions")),
                    )
                elif data["action"] == "cancel":
                    operation = cancel_bulk(
                        request.user, pk, expected_version=data["expected_version"]
                    )
                else:
                    operation = resume_bulk(
                        request.user, pk, expected_version=data["expected_version"]
                    )
            except VersionConflict as conflict:
                operation.refresh_from_db()
                error, status = str(conflict), HTTPStatus.CONFLICT
            except ValidationError as invalid:
                error = " ".join(invalid.messages)
                status = HTTPStatus.UNPROCESSABLE_CONTENT
            else:
                _dispatch(operation)
                return redirect("bulk-detail", pk=pk)
    notices = (
        operation.state == BulkOperationState.PREVIEW
        and operation.kind == BulkKind.SEND_REQUEST_NOTICES
    )
    return render(
        request,
        "cycles/bulk_detail.html",
        {
            **_context(operation),
            "error": error,
            "excluded": request.POST.getlist("exclusions"),
            "typed_count": request.POST.get("confirm_count", ""),
            "may_confirm": operation.requested_by_id == request.user.pk,
            # Read on every render, as the queue does: the firm's own reservations
            # today against the configured cap. Reading it reserves nothing.
            "remaining_quota": remaining_quota() if notices else None,
        },
        status=status,
    )


@login_required
@require_tenant
@require_http_methods(["GET"])
def bulk_progress(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Remove the polling trigger when the replacement fragment is terminal."""
    return render(
        request,
        "cycles/_bulk_progress.html",
        _context(load_operation(request.user, pk)),
    )


@login_required
@require_tenant
@require_http_methods(["GET"])
def bulk_csv(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Return only an authorized, in-memory report."""
    return bulk_report_csv(request.user, pk)

"""HTML cycle surfaces: portfolio-scoped, flag-gated and version-carrying forms."""

from http import HTTPStatus
from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import visible_clients
from apps.authz.services import can, require_can
from apps.clients.maintenance import load_client
from apps.clients.models import Engagement, TaxRegime
from apps.clients.views import require_tenant
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import (
    CycleState,
    ServiceCycle,
    TaskState,
)
from apps.cycles.services.cycle import (
    TASK_TRANSITIONS,
    add_note,
    completion_unmet,
    load_cycle,
    open_cycle,
    require_enabled,
    tenant_id,
    transition_cycle,
    transition_task,
)
from apps.cycles.workspace import (
    available_actions,
    parse_period,
    portal_projection,
)
from apps.portal.views import PortalVaultRequest


class ChangeForm(forms.Form):
    """One explicit intent and the revision rendered with that intent."""

    action = forms.ChoiceField(
        choices=[("transition", "transition"), ("task", "task"), ("note", "note")]
    )
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    to_state = forms.CharField(required=False, max_length=32)
    task_id = forms.UUIDField(required=False)
    reason = forms.CharField(required=False, max_length=10000)


class OpenForm(forms.Form):
    """Identifiers are re-authorized and eligibility checked in open_cycle."""

    engagement_id = forms.UUIDField()
    playbook_version_id = forms.UUIDField()
    periodo = forms.DateField(
        input_formats=["%Y-%m"],
        widget=forms.DateInput(attrs={"type": "month"}, format="%Y-%m"),
    )


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def cycle_list(request: AuthenticatedRequest) -> HttpResponse:
    """Filter and count the same visible queryset; no tenant-wide count leak."""
    require_enabled()
    try:
        period = parse_period(request.GET.get("periodo"))
        owner = (
            UUID(request.GET["responsavel"]) if request.GET.get("responsavel") else None
        )
    except ValueError:
        return HttpResponse("Filtros inválidos.", status=422)
    clients = visible_clients(request.user, tenant_id=tenant_id())
    rows = ServiceCycle.objects.filter(
        client_id__in=clients.values("pk"), period_start=period
    ).select_related("client", "engagement", "owner", "playbook", "tax_profile")
    owners = (
        rows.exclude(owner_id=None)
        .order_by("owner__email")
        .values_list("owner_id", "owner__email")
        .distinct()
    )
    state = request.GET.get("estado", "")
    service = request.GET.get("servico", "")
    regime = request.GET.get("regime", "")
    if state:
        rows = rows.filter(state=state)
    if service:
        rows = rows.filter(engagement__service_kind=service)
    if regime:
        rows = (
            rows.filter(Q(tax_profile__isnull=True) | Q(tax_profile__regime=regime))
            if regime == TaxRegime.UNKNOWN
            else rows.filter(tax_profile__regime=regime)
        )
    if owner:
        rows = rows.filter(owner_id=owner)
    page = Paginator(rows.order_by("client__legal_name", "pk"), 50).get_page(
        request.GET.get("pagina")
    )
    params = request.GET.copy()
    params.pop("pagina", None)
    return render(
        request,
        "cycles/cycle_list.html",
        {
            "page": page,
            "period": period,
            "states": CycleState.choices,
            "regimes": TaxRegime.choices,
            "owners": owners,
            "state": state,
            "service": service,
            "regime": regime,
            "owner_filter": str(owner) if owner else "",
            "filter_query": params.urlencode(),
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def cycle_detail(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Render the pinned graph and apply only the selected, versioned operation."""
    cycle = load_cycle(request.user, pk)
    error = ""
    status = HTTPStatus.OK
    if request.method == "POST":
        form = ChangeForm(request.POST)
        if not form.is_valid():
            error, status = (
                "Revise os dados informados.",
                HTTPStatus.UNPROCESSABLE_CONTENT,
            )
        else:
            data = form.cleaned_data
            try:
                if data["action"] == "transition":
                    transition_cycle(
                        request.user,
                        pk,
                        data["to_state"],
                        expected_version=data["expected_version"],
                        reason=data["reason"],
                    )
                elif data["action"] == "note":
                    add_note(
                        request.user,
                        pk,
                        data["reason"],
                        expected_version=data["expected_version"],
                    )
                else:
                    task = get_object_or_404(cycle.tasks, pk=data["task_id"])
                    transition_task(
                        request.user,
                        task.pk,
                        data["to_state"],
                        expected_version=data["expected_version"],
                        reason=data["reason"],
                    )
                return redirect("cycle-detail", pk=pk)
            except VersionConflict as exc:
                error, status = str(exc), HTTPStatus.CONFLICT
            except (IllegalTransition, ValidationError) as exc:
                error, status = str(exc), HTTPStatus.UNPROCESSABLE_CONTENT
    cycle.refresh_from_db()
    steps = {step.key: step for step in cycle.playbook_version.steps.all()}
    tasks = [
        {
            "task": task,
            "step": steps[task.step_key],
            "actions": [
                (value, TaskState(value).label)
                for value in TaskState.values
                if value in TASK_TRANSITIONS.get(task.state, set())
            ],
        }
        for task in sorted(
            cycle.tasks.select_related("owner"),
            key=lambda task: steps[task.step_key].position,
        )
    ]
    return render(
        request,
        "cycles/cycle_detail.html",
        {
            "cycle": cycle,
            "tasks": tasks,
            "actions": [
                (value, CycleState(value).label)
                for value in available_actions(request.user, cycle)
            ],
            "may_operate": can(request.user, CYCLES_OPERATE, tenant_id=tenant_id()),
            "unmet": completion_unmet(cycle),
            "notes": cycle.notes.select_related("author").order_by("created_at", "pk"),
            "error": error,
        },
        status=status,
    )


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can(CYCLES_OPERATE)
def cycle_open(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Open from the workspace, checking the submitted engagement belongs here."""
    require_enabled()
    load_client(request.user, tenant_id(), pk)
    form = OpenForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Dados inválidos para abrir o ciclo.", status=422)
    data = form.cleaned_data
    engagement = get_object_or_404(
        Engagement.objects.filter(client_id=pk), pk=data["engagement_id"]
    )
    try:
        ref = open_cycle(
            request.user,
            engagement.pk,
            data["periodo"],
            data["playbook_version_id"],
            idempotency_key=f"workspace:{pk}:{data['periodo']}",
        )
    except ValidationError as exc:
        return HttpResponse(str(exc), status=422)
    return redirect("cycle-detail", pk=ref.id)


@require_http_methods(["GET"])
@login_required
@require_can("clients.view_assigned")
def portal_cycle_detail(request: PortalVaultRequest, pk: UUID) -> HttpResponse:
    """Base-table SELECT only, with both RLS dimensions and explicit client scope."""
    require_enabled()
    cycle = get_object_or_404(
        ServiceCycle.objects.filter(client_id=request.client.pk), pk=pk
    )
    return render(request, "portal/cycle.html", portal_projection(cycle))

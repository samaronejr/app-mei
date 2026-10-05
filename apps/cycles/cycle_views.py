"""HTML cycle surfaces: portfolio-scoped, flag-gated and version-carrying forms."""

from http import HTTPStatus
from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import OuterRef, Q, Subquery
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import visible_clients
from apps.authz.services import can, require_can
from apps.clients.maintenance import load_client
from apps.clients.models import Engagement, TaxRegime
from apps.clients.views import require_tenant
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.evidence import SUSPENDED_STATES, DocumentIntake, ReviewDecision
from apps.cycles.exception_context import EXCEPTION_ACTION_LABELS, exception_rows
from apps.cycles.exception_evidence import exception_location
from apps.cycles.forms import SERVICE_KIND_CHOICES
from apps.cycles.models import (
    CycleState,
    EvidenceLink,
    EvidenceRequest,
    RequestState,
    ServiceCycle,
    TaskState,
)
from apps.cycles.models import Exception as CycleException
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
from apps.cycles.services.exceptions import (
    assign_exception,
    transition_exception,
)
from apps.cycles.services.proposed_transitions import propose_completion, propose_waiver
from apps.cycles.services.requests import (
    RequestFields,
    client_status,
    create_request,
    preview_recipients,
    remind_request,
    send_requests,
    update_request,
)
from apps.cycles.workspace import (
    available_actions,
    parse_period,
    portal_projection,
)
from apps.cycles.workspace_views import client_workspace
from apps.portal.request_views import client_help
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
                    if data["to_state"] in {
                        CycleState.COMPLETED,
                        CycleState.WITH_PENDING,
                    }:
                        proposal = propose_completion(
                            request.user,
                            pk,
                            to_state=data["to_state"],
                            reason=data["reason"],
                            expected_version=data["expected_version"],
                        )
                        return redirect("approval-card", pk=proposal.pk)
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
    return _render_detail(request, cycle.pk, error, status)


def _render_detail(
    request: AuthenticatedRequest,
    pk: UUID,
    error: str,
    status: int,
    selected_exception: CycleException | None = None,
) -> HttpResponse:
    """Render the cycle page; shared by the cycle and the request-action posts."""
    # Refresh state after a refused POST and fetch the visible context together,
    # rather than adding a client/engagement query for every header field.
    cycle = ServiceCycle.objects.select_related(
        "client", "engagement", "playbook", "playbook_version"
    ).get(pk=pk)
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
            "service_label": dict(SERVICE_KIND_CHOICES)[cycle.engagement.service_kind],
            "tasks": tasks,
            "actions": [
                (value, CycleState(value).label)
                for value in available_actions(request.user, cycle)
            ],
            "may_operate": can(request.user, CYCLES_OPERATE, tenant_id=tenant_id()),
            "unmet": completion_unmet(cycle),
            "notes": cycle.notes.select_related("author").order_by("created_at", "pk"),
            "proposals": cycle.proposals.order_by("-created_at", "-pk"),
            "exceptions": exception_rows(
                request,
                CycleException.objects.filter(cycle=cycle),
                "ciclo",
                selected_exception,
                error,
            ),
            "error": error,
            **_requests_context(request, cycle),
        },
        status=status,
    )


def _intake_state(link: EvidenceLink) -> str | None:
    """Read the `intake_state` annotation (None: a vault upload without intake)."""
    state: str | None = link.__dict__.get("intake_state")
    return state


def _requests_context(
    request: AuthenticatedRequest, cycle: ServiceCycle
) -> dict[str, object]:
    """Build the firm's view of the cycle's requests and the send preview (T11)."""
    rows = list(
        EvidenceRequest.objects.filter(cycle=cycle)
        .prefetch_related("internals")
        .order_by("created_at", "pk")
    )
    links: dict[UUID, list[EvidenceLink]] = {}
    for link in (
        EvidenceLink.objects.filter(request__cycle=cycle)
        .select_related("document")
        # The intake state rides on the same query: no per-link lookup (T12).
        .annotate(
            intake_state=Subquery(
                DocumentIntake.objects.filter(document=OuterRef("document_id")).values(
                    "state"
                )[:1]
            )
        )
        .order_by("created_at", "pk")
    ):
        links.setdefault(link.request_id, []).append(link)
    # A refused file (type or malware), a document of another company and a file
    # the reviewer did not accept are shown to the firm but are not "sent" (T12/T13).
    counted = {
        request_id
        for request_id, rows in links.items()
        if any(
            _intake_state(link) not in SUSPENDED_STATES
            and link.review_decision != ReviewDecision.REJECTED
            for link in rows
        )
    }
    today = timezone.localdate()
    return {
        "evidence_requests": [
            {
                "request": row,
                "internal": next(iter(row.internals.all()), None),
                "links": links.get(row.pk, []),
                "status": client_status(row, submitted=row.pk in counted, today=today),
                "editable": row.state in {RequestState.DRAFT, RequestState.REQUESTED},
                "closable": row.state
                not in {
                    RequestState.WAIVED,
                    RequestState.ACCEPTED,
                    RequestState.REPLACED,
                },
            }
            for row in rows
        ],
        "draft_count": sum(1 for row in rows if row.state == RequestState.DRAFT),
        "recipients": preview_recipients(request.user, cycle.pk),
    }


class RequestActionForm(forms.Form):
    """One request command and the revision its form was rendered with."""

    action = forms.ChoiceField(
        choices=[
            ("create", "create"),
            ("update", "update"),
            ("waive", "waive"),
            ("send", "send"),
            ("remind", "remind"),
        ]
    )
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    request_id = forms.UUIDField(required=False)
    title = forms.CharField(required=False, max_length=160)
    client_note = forms.CharField(required=False, max_length=2000)
    due_on = forms.DateField(required=False)
    instructions = forms.CharField(required=False, max_length=2000)
    reason = forms.CharField(required=False, max_length=2000)


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can(CYCLES_OPERATE)
def cycle_request_action(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Apply one versioned request command; the services re-check scope and version."""
    cycle = load_cycle(request.user, pk)
    form = RequestActionForm(request.POST)
    if not form.is_valid():
        return _render_detail(
            request,
            cycle.pk,
            "Revise os dados da solicitação.",
            HTTPStatus.UNPROCESSABLE_CONTENT,
        )
    data = form.cleaned_data
    fields = RequestFields(
        title=data["title"],
        client_note=data["client_note"],
        due_on=data["due_on"],
        instructions=data["instructions"],
    )
    version = data["expected_version"]
    try:
        if data["action"] == "create":
            create_request(request.user, cycle.pk, fields, expected_version=version)
        elif data["action"] == "send":
            send_requests(request.user, cycle.pk, expected_version=version)
        else:
            target = get_object_or_404(
                EvidenceRequest.objects.filter(cycle=cycle), pk=data["request_id"]
            )
            if data["action"] == "update":
                update_request(
                    request.user, target.pk, fields, expected_version=version
                )
            elif data["action"] == "waive":
                proposal = propose_waiver(
                    request.user,
                    target.pk,
                    reason=data["reason"],
                    expected_version=version,
                )
                return redirect("approval-card", pk=proposal.pk)
            else:
                remind_request(request.user, target.pk, expected_version=version)
    except VersionConflict as exc:
        return _render_detail(request, cycle.pk, str(exc), HTTPStatus.CONFLICT)
    except (IllegalTransition, ValidationError) as exc:
        message = (
            "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        )
        return _render_detail(
            request, cycle.pk, message, HTTPStatus.UNPROCESSABLE_CONTENT
        )
    return redirect(f"{reverse('cycle-detail', args=[cycle.pk])}#solicitacoes")


class ExceptionActionForm(forms.Form):
    """One exception command and the exception revision the drawer rendered."""

    action = forms.ChoiceField(
        choices=[
            ("assign", "assign"),
            *[(state, state) for state in EXCEPTION_ACTION_LABELS],
        ]
    )
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    owner_id = forms.UUIDField(required=False)
    note = forms.CharField(required=False, max_length=2000)
    origin = forms.ChoiceField(
        choices=[("ciclo", "ciclo"), ("cliente", "cliente")], required=False
    )


def _render_exception_surface(
    request: AuthenticatedRequest,
    problem: CycleException,
    origin: str,
    error: str,
    status: int,
) -> HttpResponse:
    if origin == "cliente":
        return client_workspace(request, problem.client_id, problem, error, status)
    return _render_detail(request, problem.cycle_id, error, status, problem)


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can(CYCLES_OPERATE)
def cycle_exception_action(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Apply one versioned exception command; 409 when the drawer was stale."""
    require_enabled()
    problem = get_object_or_404(CycleException.objects.select_related("cycle"), pk=pk)
    cycle = load_cycle(request.user, problem.cycle_id)
    origin = request.POST.get("origin") or request.GET.get("origem", "ciclo")
    origin = "cliente" if origin == "cliente" else "ciclo"
    if request.method == "GET":
        return redirect(exception_location(cycle, problem.pk, origin))
    form = ExceptionActionForm(request.POST)
    data = form.cleaned_data if form.is_valid() else None
    if data is None or (data["action"] == "assign" and data["owner_id"] is None):
        return _render_exception_surface(
            request,
            problem,
            origin,
            "Revise os dados da pendência.",
            HTTPStatus.UNPROCESSABLE_CONTENT,
        )
    try:
        if data["action"] == "assign":
            assign_exception(
                request.user,
                problem.pk,
                data["owner_id"],
                expected_version=data["expected_version"],
            )
        else:
            transition_exception(
                request.user,
                problem.pk,
                data["action"],
                expected_version=data["expected_version"],
                note=data["note"],
            )
    except VersionConflict as exc:
        return _render_exception_surface(
            request, problem, origin, str(exc), HTTPStatus.CONFLICT
        )
    except (IllegalTransition, ValidationError) as exc:
        message = (
            "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        )
        return _render_exception_surface(
            request, problem, origin, message, HTTPStatus.UNPROCESSABLE_CONTENT
        )
    return redirect(exception_location(cycle, problem.pk, origin))


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
    return render(
        request,
        "portal/cycle.html",
        {
            "cycles_r1": True,
            "help_rows": client_help(cycle.pk),
            **portal_projection(cycle),
        },
    )

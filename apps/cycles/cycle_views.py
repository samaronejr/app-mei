"""HTML cycle surfaces: portfolio-scoped, flag-gated and version-carrying forms."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.accounts.views import AuthenticatedRequest
from apps.authz.portfolio import visible_clients
from apps.authz.services import can, require_can
from apps.clients.maintenance import load_client
from apps.clients.models import Engagement, TaxRegime
from apps.clients.views import require_tenant
from apps.core.flags import tenant_has
from apps.core.refusal import WayBack, refusal_page, sentences
from apps.cycles.capabilities import CYCLES_OPERATE, CYCLES_REOPEN
from apps.cycles.display_completion import display_completion_items
from apps.cycles.display_reads import load_cycle_detail
from apps.cycles.display_requests import display_internal
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.evidence import SUSPENDED_STATES, ReviewDecision
from apps.cycles.exception_context import (
    EXCEPTION_ACTION_LABELS,
    display_exception_rows,
)
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
from apps.cycles.scanning import scan_required
from apps.cycles.services.cycle import (
    TASK_TRANSITIONS,
    add_note,
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
from apps.obligations.observations import (
    StaffFact,
    StaleObservationError,
    record_cycle_fact,
)
from apps.portal.request_views import client_help, open_requests
from apps.portal.views import PortalVaultRequest

if TYPE_CHECKING:
    from apps.cycles.display_reads import CycleDetailReads


class ChangeForm(forms.Form):
    """One explicit intent and the revision rendered with that intent."""

    action = forms.ChoiceField(
        choices=[
            ("transition", "transition"),
            ("task", "task"),
            ("note", "note"),
            ("observation", "observation"),
        ]
    )
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    to_state = forms.CharField(required=False, max_length=32)
    task_id = forms.UUIDField(required=False)
    reason = forms.CharField(required=False, max_length=10000)
    # T17 staff-fact fields; only the observation action reads them.
    op = forms.ChoiceField(
        choices=[("record", "record"), ("correct", "correct"), ("retract", "retract")],
        required=False,
    )
    subject_id = forms.UUIDField(required=False)
    kind = forms.ChoiceField(
        choices=[("payment", "payment"), ("guide_issued", "guide_issued")],
        required=False,
    )
    head_id = forms.UUIDField(required=False)
    source_ref = forms.CharField(required=False, max_length=255)
    evidence_link_id = forms.UUIDField(required=False)


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
        return refusal_page(
            request,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            heading="Não foi possível filtrar os ciclos",
            reason="Filtros inválidos.",
            back=WayBack(reverse("cycle-list"), "Voltar aos ciclos"),
        )
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
            "may_bulk": tenant_has(tenant_id(), "bulk_r1")
            and not request.user.is_superuser
            and can(request.user, "bulk.run", tenant_id=tenant_id()),
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def cycle_detail(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Render the pinned graph and apply only the selected, versioned operation."""
    if request.method == "GET":
        return _render_detail(request, pk, "", HTTPStatus.OK)
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
                            reason={
                                key.removeprefix("pending_reason:"): str(
                                    request.POST[key]
                                )
                                for key in request.POST
                                if key.startswith("pending_reason:")
                                and str(request.POST[key]).strip()
                            }
                            or data["reason"],
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
                elif data["action"] == "observation":
                    _observation_action(request.user, cycle, data)
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
            except (VersionConflict, StaleObservationError) as exc:
                error, status = sentences(exc), HTTPStatus.CONFLICT
            except (IllegalTransition, ValidationError) as exc:
                error, status = sentences(exc), HTTPStatus.UNPROCESSABLE_CONTENT
    return _render_detail(request, cycle.pk, error, status)


def _render_detail(
    request: AuthenticatedRequest,
    pk: UUID,
    error: str,
    status: int,
    selected_exception: CycleException | None = None,
) -> HttpResponse:
    """Render the cycle page; shared by the cycle and the request-action posts."""
    # A new display cut after a refused POST keeps rendered versions fresh. No
    # command receives this projection; services still reload and lock their scope.
    reads = load_cycle_detail(request.user, pk)
    cycle = reads.cycle
    steps = {step.key: step for step in reads.steps}
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
            reads.tasks,
            key=lambda task: steps[task.step_key].position,
        )
    ]
    missing_items = display_completion_items(reads)
    unmet = list(dict.fromkeys(item.code for item in missing_items))
    checklist = [
        {
            "label": label,
            "passed": code not in unmet,
            "href": href,
        }
        for code, label, href in (
            ("tasks", "Tarefas aceitas ou dispensadas", "#graph-title"),
            (
                "requests",
                "Solicitações obrigatórias aceitas ou dispensadas com aprovação",
                "#solicitacoes",
            ),
            (
                "exceptions",
                "Nenhuma pendência aberta bloqueia a conclusão",
                "#pendencias",
            ),
            (
                "actions",
                "Efeitos e comunicações com resultado definido",
                "#cycle-proposals",
            ),
            (
                "evidence_index",
                "Índice de evidências atualizado",
                reverse("evidence-index", args=[cycle.pk]),
            ),
        )
    ]
    return render(
        request,
        "cycles/cycle_detail.html",
        {
            "cycle": cycle,
            "coverage": reads.coverage,
            "service_label": dict(SERVICE_KIND_CHOICES)[cycle.engagement.service_kind],
            "tasks": tasks,
            "actions": [
                (value, CycleState(value).label)
                for value in available_actions(
                    request.user, cycle, unmet=unmet, allowed=reads.allowed
                )
            ],
            "may_operate": CYCLES_OPERATE in reads.allowed,
            "unmet": unmet,
            "completion_checklist": checklist,
            "completion_items": missing_items,
            "may_reopen": cycle.state in {CycleState.COMPLETED, CycleState.WITH_PENDING}
            and CYCLES_REOPEN in reads.allowed,
            "notes": reads.notes,
            "proposals": reads.proposals,
            "exceptions": display_exception_rows(
                request, reads, selected_exception, error
            ),
            "error": error,
            "scan_required": scan_required(cycle.tenant_id),
            **_requests_context(reads),
        },
        status=status,
    )


def _observation_action(
    actor: User, cycle: ServiceCycle, data: Mapping[str, Any]
) -> None:
    """Write the staff fact the Cobertura panel posted (T17).

    `subject_id` is resolved through the actor's portfolio inside the service;
    the period is always the cycle's own competence so a fact can never land
    on a month the panel did not show. A missing head on a correct/retract is
    a form error; a head that moved is a stale-snapshot conflict.
    """
    if data["subject_id"] is None or not data["op"] or not data["kind"]:
        msg = "Informe a obrigação, o fato e a ação."
        raise ValidationError(msg)
    record_cycle_fact(
        actor,
        cycle.pk,
        StaffFact(
            subject_id=data["subject_id"],
            kind=data["kind"],
            operation=data["op"],
            source_ref=data["source_ref"],
            expected_head_id=data["head_id"],
            evidence_link_id=data["evidence_link_id"],
        ),
        expected_version=data["expected_version"],
    )


def _intake_state(link: EvidenceLink) -> str | None:
    """Read the `intake_state` annotation (None: a vault upload without intake)."""
    state: str | None = link.__dict__.get("intake_state")
    return state


def _requests_context(reads: "CycleDetailReads") -> dict[str, object]:
    """Render requests and recipient access from the same display-only cut."""
    rows = reads.requests
    links: dict[UUID, list[EvidenceLink]] = {}
    for link in reads.links:
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
                "internal": display_internal(row),
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
        "observation_evidence": [
            {
                "id": link.pk,
                "obligation_id": row.obligation_id,
                "filename": link.document.original_filename,
                "created_at": link.created_at,
            }
            for row in rows
            for link in links.get(row.pk, [])
        ],
        "recipients": reads.recipients,
        # The only legal receipt bindings — the service re-checks inside the
        # aggregate lock; this list just keeps the form honest.
        "bindable_obligations": reads.obligations,
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
    # T17: receipt binding; always submitted (defaults echo the stored row), so
    # a non-draft edit simply replays them and the service sees no change.
    kind = forms.ChoiceField(
        choices=[("documento", "documento"), ("comprovante", "comprovante")],
        required=False,
    )
    obligation_id = forms.UUIDField(required=False)


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
        kind=data["kind"] or "documento",
        obligation_id=data["obligation_id"],
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
    heading = "Não foi possível abrir o ciclo"
    workspace = reverse("client-detail", args=[pk])
    form = OpenForm(request.POST)
    if not form.is_valid():
        return refusal_page(
            request,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            heading=heading,
            reason="Dados inválidos para abrir o ciclo.",
            back=WayBack(workspace, "Voltar ao espaço do cliente"),
        )
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
        return refusal_page(
            request,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            heading=heading,
            reason=" ".join(exc.messages),
            back=WayBack(
                f"{workspace}?periodo={data['periodo']:%Y-%m}",
                "Voltar ao espaço do cliente",
            ),
        )
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
    requests = open_requests(timezone.localdate(), cycle_id=cycle.pk)
    next_request = next((row for row in requests if row.actionable), None)
    return render(
        request,
        "portal/cycle.html",
        {
            "cycles_r1": True,
            "help_rows": client_help(cycle.pk),
            "next_request": next_request,
            "requests": requests,
            **portal_projection(
                cycle,
                has_actionable_requests=next_request is not None,
                has_submitted_requests=any(
                    row.status == "recebido" for row in requests
                ),
            ),
        },
    )

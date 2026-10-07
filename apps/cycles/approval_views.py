"""Firm-only frozen cards; each POST repeats live authorization in the service."""

import json
from datetime import datetime, timedelta
from http import HTTPStatus
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.approvals import may_configure_policy, proposal_controls
from apps.authz.services import require_can
from apps.clients.maintenance import load_client
from apps.clients.views import require_tenant
from apps.cycles.approval_forms import (
    OBSERVATION_SOURCE_LABELS,
    ApprovalPolicyForm,
    DecisionForm,
    ProposalForm,
    observation_status_label,
)
from apps.cycles.effects.build import BuildError, initiation_capability
from apps.cycles.effects.executors import ExecutionResult, execute
from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import ActionExecution, ActionProposal, ProposalState
from apps.cycles.services.approvals import (
    DecisionResult,
    approve,
    cancel,
    configure_approval_policy,
    propose,
    reject,
    revalidate,
)
from apps.cycles.services.cycle import load_cycle, require_enabled, tenant_id
from apps.delivery.errors import IntentConflict
from apps.tenants.models import Tenant

REASON_LABELS = {
    "recipients_changed": "Os destinatários mudaram.",
    "recipient_ineligible": "Um destinatário não está mais autorizado.",
    "recipient_unavailable": "Um destinatário selecionado não está mais disponível.",
    "documents_changed": "Os documentos ou a revisão mudaram.",
    "document_not_reviewed": "Um documento não está mais verificado e aceito.",
    "document_unavailable": "Um documento selecionado não está mais disponível.",
    "evidence_changed": "A evidência selecionada mudou.",
    "evidence_unavailable": "Uma solicitação selecionada não está mais disponível.",
    "amount_changed": "Um valor mudou.",
    "amount_invalid": "Um valor extraído precisa de revisão.",
    "amount_unavailable": "Um valor selecionado não está mais disponível.",
    "policy_changed": "A política ou a condição de autoaprovação mudou.",
    "approver_role_revoked": "Um aprovador perdeu a autoridade necessária.",
    "approver_authority_changed": "A autoridade de um aprovador mudou.",
    "authorization_changed": "A autoridade de quem propôs mudou.",
    "initiator_authority_revoked": "Quem propôs perdeu a autoridade necessária.",
    "grant_or_support_expired": "Uma autorização expirou.",
    "approval_expired": "Uma decisão de aprovação expirou.",
    "source_stale": "Uma observação de fonte está desatualizada.",
    "source_changed": "A fonte selecionada mudou.",
    "observation_superseded": "Uma observação foi substituída por outra.",
    "observation_unavailable": "Uma observação selecionada não está mais disponível.",
    "observation_period_mismatch": "A observação não pertence à competência do ciclo.",
    "proposal_expired": "A validade desta proposta terminou.",
    "body_text_changed": "O texto da mensagem mudou.",
    "manifest_identity_changed": "O contexto fixado da proposta mudou.",
    "payload_changed": "O efeito proposto mudou.",
    "not_before": "O horário mínimo de execução ainda não chegou.",
    "effect_context_closed": "O cliente ou o ciclo está encerrado.",
    "engagement_changed": "A vigência do serviço mudou.",
}


def _proposal(request: AuthenticatedRequest, pk: UUID) -> ActionProposal:
    require_enabled()
    proposal = get_object_or_404(
        ActionProposal.objects.select_related("client", "cycle", "initiator"), pk=pk
    )
    load_client(request.user, tenant_id(), proposal.client_id)
    return proposal


def _decision(
    request: AuthenticatedRequest, proposal: ActionProposal
) -> HttpResponse | None:
    form = DecisionForm(request.POST)
    if not form.is_valid():
        return _card(request, proposal, "Revise os dados informados.", 422)
    data = form.cleaned_data
    result: DecisionResult | ExecutionResult
    try:
        if data["action"] == "approve":
            result = approve(
                request.user,
                proposal.pk,
                expected_version=data["expected_version"],
                self_approval_acknowledged=data["self_approval_acknowledged"],
            )
        elif data["action"] == "reject":
            result = reject(
                request.user,
                proposal.pk,
                expected_version=data["expected_version"],
                reason=data["reason"],
            )
        elif data["action"] == "cancel":
            result = cancel(
                request.user, proposal.pk, expected_version=data["expected_version"]
            )
        elif data["action"] == "execute":
            result = execute(
                request.user, proposal.pk, expected_version=data["expected_version"]
            )
            if not result.executed:
                proposal.refresh_from_db()
                return _card(
                    request,
                    proposal,
                    (
                        "A execução não está autorizada. "
                        "Confira a validade e as aprovações."
                    ),
                    HTTPStatus.CONFLICT,
                )
        else:
            result = revalidate(
                request.user, proposal.pk, expected_version=data["expected_version"]
            )
    except VersionConflict as error:
        proposal.refresh_from_db()
        return _card(request, proposal, str(error), HTTPStatus.CONFLICT)
    except (ValidationError, IllegalTransition, IntentConflict) as error:
        return _card(request, proposal, str(error), HTTPStatus.UNPROCESSABLE_CONTENT)
    if result.proposal.state == ProposalState.INVALIDATED:
        proposal.refresh_from_db()
        return _card(
            request,
            proposal,
            "Esta versão perdeu a validade. Crie uma nova versão para decidir.",
            HTTPStatus.CONFLICT,
        )
    return None


def _card(
    request: AuthenticatedRequest,
    proposal: ActionProposal,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    controls = proposal_controls(
        request.user,
        tenant_id(),
        initiation_capability(ActionKind(proposal.action_kind)),
    )
    frozen = json.loads(bytes(proposal.manifest_bytes))
    expires_at = datetime.fromisoformat(frozen["expires_at"])
    now = timezone.now()
    dates = {
        "expires_at": expires_at,
        "not_before": (
            datetime.fromisoformat(frozen["not_before"])
            if frozen["not_before"]
            else None
        ),
    }
    observations = [
        {
            **row,
            "source_label": OBSERVATION_SOURCE_LABELS.get(
                row["source_kind"], row["source_kind"]
            ),
            "status_label": observation_status_label(row["source_kind"], row["status"]),
            "observed_at": datetime.fromisoformat(row["observed_at"]),
            "stale": row["status"] == "stale"
            or (
                row["valid_until"] is not None
                and datetime.fromisoformat(row["valid_until"]) <= now
            ),
        }
        for row in frozen["observations"]
    ]
    execution = ActionExecution.objects.filter(proposal=proposal).first()
    action_label = (
        "Reabrir ciclo"
        if frozen["payload"].get("reopen")
        else proposal.get_action_kind_display()
    )
    return render(
        request,
        "cycles/approval_card.html",
        {
            "proposal": proposal,
            "action_label": action_label,
            "execution": execution,
            "may_execute": (
                controls.propose
                and proposal.state == ProposalState.APPROVED
                and execution is None
                and expires_at > now
            ),
            "frozen": frozen,
            "dates": dates,
            "expired": expires_at <= now,
            "observations": observations,
            "error": error,
            "warnings": [
                REASON_LABELS.get(code, code) for code in proposal.invalidation_reasons
            ],
            "decisions": proposal.approvals.select_related("approver").order_by(
                "decided_at"
            ),
            "may_approve": controls.approve,
            "may_cancel": (
                controls.propose
                and (proposal.initiator_id == request.user.pk or controls.cancel_others)
            ),
            "may_propose": controls.propose,
            "self_approval": proposal.initiator_id == request.user.pk,
            "awaiting": proposal.state == ProposalState.AWAITING,
            "open": proposal.state in {ProposalState.AWAITING, ProposalState.APPROVED},
            "effects": [
                {
                    "label": action_label,
                    "detail": (
                        "Somente os destinatários, documentos e valores "
                        "congelados abaixo."
                    ),
                }
            ],
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
def approval_card(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Show stored bytes as facts; GET never silently changes decision state."""
    # _proposal's load_client enforces live view_assigned plus portfolio access.
    # Controls and commands each resolve their own separate authorization question.
    proposal = _proposal(request, pk)
    if request.method == "POST":
        refused = _decision(request, proposal)
        if refused is not None:
            return refused
        return redirect("approval-card", pk=pk)
    return _card(request, proposal)


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_can("clients.view_assigned")
def proposal_create(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Choose sources, freeze them, then navigate to the real approval card."""
    cycle = load_cycle(request.user, pk)
    initial: dict[str, object] = {
        "expected_version": cycle.version,
        "expires_at": timezone.localtime(timezone.now() + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
    }
    previous_id = request.GET.get("revisao_de")
    if previous_id:
        try:
            previous_key = UUID(previous_id)
        except ValueError:
            return HttpResponse(
                "Identificador de versão inválido.",
                status=HTTPStatus.UNPROCESSABLE_CONTENT,
            )
        previous = get_object_or_404(
            ActionProposal.objects, pk=previous_key, cycle=cycle
        )
        initial.update(previous.input_spec)
        initial.update(
            {
                "previous_id": previous.pk,
                "previous_version": previous.version,
                "money": [
                    f"{item.link_id}:{item.field_name}"
                    for item in ProposalInput.from_data(previous.input_spec).money
                ],
            }
        )
    form = ProposalForm(
        request.POST if request.method == "POST" else None, initial=initial
    )
    form.set_choices(cycle)
    status = HTTPStatus.OK
    if request.method == "POST" and form.is_valid():
        try:
            proposal = propose(
                request.user,
                cycle.pk,
                form.draft(),
                expected_version=form.cleaned_data["expected_version"],
            )
            return redirect("approval-card", pk=proposal.pk)
        except VersionConflict as error:
            form.add_error(None, str(error))
            status = HTTPStatus.CONFLICT
        except (ValidationError, IllegalTransition, BuildError) as error:
            message = REASON_LABELS.get(str(error), str(error))
            form.add_error(None, message)
            status = HTTPStatus.UNPROCESSABLE_CONTENT
    elif request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_CONTENT
    return render(
        request,
        "cycles/proposal_form.html",
        {
            "cycle": cycle,
            "form": form,
            "may_configure": may_configure_policy(request.user, tenant_id()),
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
def approval_policy(request: AuthenticatedRequest) -> HttpResponse:
    """Require the ordinary firm owner for the explicit self-approval setting."""
    require_enabled()
    if not may_configure_policy(request.user, tenant_id()):
        raise PermissionDenied
    # PLATFORM_QUERY_OK: one explicit policy already gated to the current owner.
    tenant = Tenant.objects.get(pk=tenant_id())
    form = ApprovalPolicyForm(
        request.POST if request.method == "POST" else None,
        initial={
            "expected_version": tenant.approval_policy_version,
            "allow_self_approval": tenant.allow_self_approval,
            "quorum": tenant.approval_quorum,
        },
    )
    status = HTTPStatus.OK
    if request.method == "POST" and form.is_valid():
        try:
            configure_approval_policy(request.user, **form.cleaned_data)
            return redirect("approval-policy")
        except VersionConflict as error:
            form.add_error(None, str(error))
            status = HTTPStatus.CONFLICT
        except ValidationError as error:
            form.add_error(None, error)
            status = HTTPStatus.UNPROCESSABLE_CONTENT
    elif request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_CONTENT
    return render(
        request,
        "cycles/approval_policy.html",
        {"form": form, "tenant": tenant},
        status=status,
    )

"""Firm-only frozen cards; each POST repeats live authorization in the service."""

import json
from datetime import datetime, timedelta
from enum import StrEnum, auto
from http import HTTPStatus
from typing import Any
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
from apps.authz.support import in_support_mode
from apps.clients.maintenance import load_client
from apps.clients.models import ClientContact
from apps.clients.views import require_tenant
from apps.core.refusal import sentences
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


EMPTY_SECTION_LABELS = (
    ("recipients", "destinatários externos"),
    ("money", "valores monetários"),
    ("documents", "documentos"),
    ("evidence", "solicitações de evidência"),
    ("observations", "observações de fonte"),
)


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
        return _card(
            request, proposal, sentences(error), HTTPStatus.UNPROCESSABLE_CONTENT
        )
    if result.proposal.state == ProposalState.INVALIDATED:
        proposal.refresh_from_db()
        return _card(
            request,
            proposal,
            "Esta versão perdeu a validade. Crie uma nova versão para decidir.",
            HTTPStatus.CONFLICT,
        )
    return None


# Message bodies never reach a support session: the frozen manifest is the
# proposal's own archive, and the support grant buys the page, not the mail.
# Structural fields stay untouched — digests, identifiers and decisions are what
# make the card audit-complete; bodies, headers and addressees are the payload
# a support visit must not carry away.
REDACTED_PAYLOAD_FIELDS = frozenset(
    {"subject", "body", "from", "headers", "recipients"}
)


class BadgeState(StrEnum):
    """The states partials/ui/status_badge.html draws that an approval card uses."""

    PENDING = auto()
    MANUAL = auto()
    BLOCKED = auto()
    STALE = auto()
    UNKNOWN = auto()


# An approval is a human decision, never verification, so no state borrows green.
PROPOSAL_STATE_BADGES: dict[str, BadgeState] = {
    ProposalState.AWAITING: BadgeState.PENDING,
    ProposalState.APPROVED: BadgeState.MANUAL,
    ProposalState.REJECTED: BadgeState.BLOCKED,
    ProposalState.INVALIDATED: BadgeState.STALE,
    ProposalState.CANCELLED: BadgeState.BLOCKED,
}


def _redact_frozen(frozen: dict[str, Any]) -> dict[str, Any]:
    """Return a manifest copy with message bodies and addressees replaced."""
    payload = frozen.get("payload")
    if not isinstance(payload, dict):
        return frozen
    redacted = dict(payload)
    for field in REDACTED_PAYLOAD_FIELDS:
        if field in redacted:
            redacted[field] = "•••" if field != "recipients" else []
    notice = redacted.get("notice")
    if isinstance(notice, dict):
        redacted["notice"] = {
            **notice,
            **{
                field: "•••" for field in ("subject", "body", "from") if field in notice
            },
        }
    return {**frozen, "payload": redacted}


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
    if in_support_mode():
        frozen = _redact_frozen(frozen)
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
    # Names identify the frozen contacts for the reader; the frozen address stays
    # the fact. A support visit resolves none, as it carries no addressee away.
    names = (
        {}
        if in_support_mode()
        else {
            str(pk): name
            for pk, name in ClientContact.objects.filter(
                client_id=proposal.client_id,
                pk__in={row["contact_id"] for row in frozen["recipients"]},
            ).values_list("pk", "name")
        }
    )
    recipients = [
        {**row, "name": names.get(row["contact_id"], "")}
        for row in frozen["recipients"]
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
            "state_badge": PROPOSAL_STATE_BADGES.get(
                proposal.state, BadgeState.UNKNOWN
            ),
            "recipients": recipients,
            "dates": dates,
            "expired": expires_at <= now,
            "observations": observations,
            "error": error,
            # A 409 is a stale view, not a refusal: the card says so differently.
            "conflict": status == HTTPStatus.CONFLICT,
            # Frozen collections with nothing in them share one compact line.
            "empty_sections": [
                label for key, label in EMPTY_SECTION_LABELS if not frozen[key]
            ],
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
            # REASON_LABELS is keyed by machine code, which is what a BuildError's
            # text is; a ValidationError is looked up by its joined messages and
            # otherwise shown as those sentences, never as its list repr.
            said = sentences(error)
            form.add_error(None, REASON_LABELS.get(said, said))
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

"""One authoritative checklist shared by presentation and locked completion."""

import json
from dataclasses import dataclass

from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    ApprovalDecision,
    CycleState,
    CycleTask,
    EvidenceRequest,
    ExceptionState,
    ProposalState,
    RequestInternal,
    RequestState,
    ServiceCycle,
    TaskState,
)
from apps.cycles.models import Exception as CycleException
from apps.delivery.models import MessageState, OutboundMessage


@dataclass(frozen=True, slots=True)
class CompletionItem:
    """Identify one unmet condition by subject revision and its resolution link."""

    key: str
    code: str
    label: str
    href: str


def valid_waiver(request: EvidenceRequest) -> bool:
    """Require the exact executed approval that produced this waiver revision."""
    internal = RequestInternal.objects.filter(request=request).first()
    if internal is None or not internal.waiver_reason.strip():
        return False
    receipts = ActionExecution.objects.filter(
        client_id=request.client_id,
        cycle_id=request.cycle_id,
        action_kind=ActionKind.REQUEST_WAIVER,
        proposal__input_spec__evidence_requests__contains=[str(request.pk)],
    ).select_related("proposal")
    for receipt in receipts:
        proposal = receipt.proposal
        manifest = json.loads(bytes(proposal.manifest_bytes))
        if (
            manifest["payload"]["reason"] == internal.waiver_reason
            and any(
                item["request_id"] == str(request.pk)
                and item["revision"] == request.version - 1
                for item in manifest["evidence"]
            )
            and proposal.approvals.filter(
                decision=ApprovalDecision.APPROVE,
                digest=receipt.digest,
                proposal_revision=receipt.proposal_revision,
                expires_at__gt=receipt.executed_at,
            ).exists()
        ):
            return True
    return False


def completion_items(cycle: ServiceCycle) -> list[CompletionItem]:
    """Return every current blocker without exempting unrelated completion actions."""
    from apps.cycles.services.cycle import COMPLETION_GUARDS  # noqa: PLC0415

    items: list[CompletionItem] = []
    base = f"/ciclos/{cycle.pk}/"
    items.extend(
        CompletionItem(
            f"tasks:{task.pk}:{task.version}",
            "tasks",
            "Tarefa ainda não aceita ou dispensada",
            base + "#graph-title",
        )
        for task in CycleTask.objects.filter(cycle=cycle)
        .exclude(state__in=[TaskState.ACCEPTED, TaskState.WAIVED])
        .order_by("pk")
    )
    for request in (
        EvidenceRequest.objects.filter(cycle=cycle, required=True)
        .exclude(state=RequestState.ACCEPTED)
        .order_by("pk")
    ):
        if request.state == RequestState.WAIVED and valid_waiver(request):
            continue
        items.append(
            CompletionItem(
                f"requests:{request.pk}:{request.version}",
                "requests",
                "Solicitação obrigatória sem aceite ou dispensa aprovada",
                base + f"#solicitacao-{request.pk}",
            )
        )
    items.extend(
        CompletionItem(
            f"exceptions:{exception.pk}:{exception.version}",
            "exceptions",
            "Pendência aberta bloqueia a conclusão",
            base + "#pendencias",
        )
        for exception in CycleException.objects.filter(
            cycle=cycle,
            blocks_completion=True,
            state__in=[ExceptionState.OPEN, ExceptionState.HANDLING],
        ).order_by("pk")
    )
    items.extend(
        CompletionItem(
            f"actions:{proposal.pk}:{proposal.version}",
            "actions",
            "Efeito ainda não encerrado",
            f"/acoes/{proposal.pk}/",
        )
        for proposal in ActionProposal.objects.filter(
            cycle=cycle, executions__isnull=True
        )
        .exclude(
            state__in=[
                ProposalState.REJECTED,
                ProposalState.CANCELLED,
                ProposalState.INVALIDATED,
            ]
        )
        .order_by("pk")
    )
    items.extend(
        CompletionItem(
            f"actions:{message.pk}:{message.version}",
            "actions",
            "Comunicação sem resultado definido",
            "/comunicacao/",
        )
        for message in OutboundMessage.objects.filter(
            cycle=cycle,
            state__in=[
                MessageState.QUEUED,
                MessageState.SENDING,
                MessageState.TEMPORARY,
                MessageState.UNKNOWN,
            ],
        ).order_by("pk")
    )
    guard = COMPLETION_GUARDS.get("evidence_index")
    if guard is None or not guard(cycle):
        items.append(
            CompletionItem(
                f"evidence_index:{cycle.pk}:{cycle.effect_revision}",
                "evidence_index",
                "Gere um índice atualizado das evidências",
                base + "evidencias/",
            )
        )
    return items


def require_open_completion(cycle: ServiceCycle, target: str) -> bool:
    """Use the same terminal transition table in the direct service and executor."""
    from apps.cycles.services.cycle import TRANSITIONS  # noqa: PLC0415

    return target in {CycleState.COMPLETED, CycleState.WITH_PENDING} and (
        target in TRANSITIONS[cycle.state]
    )

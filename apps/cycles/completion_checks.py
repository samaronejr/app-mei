"""One authoritative checklist shared by presentation and locked completion."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from django.db.models import Exists, OuterRef

from apps.cycles.effects.manifest import ActionKind
from apps.cycles.effects.markers import MarkerState, proving, with_marker_state
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
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


def waiver_proofs(
    cycle: ServiceCycle, waived: Mapping[UUID, tuple[int, str]]
) -> frozenset[UUID]:
    """Which waived requests the exact executed approval of their revision proves.

    `waived` maps each waived request to (current version, private waiver reason).
    One query reads every proving waiver receipt with its approval; the match is in
    memory: nonblank equal reason, the request selected and frozen at version - 1,
    and an approval of the same revision and digest still valid when it executed.
    """
    candidates = {pk: row for pk, row in waived.items() if row[1].strip()}
    if not candidates:
        return frozenset()
    approvals = Approval.objects.filter(
        proposal_id=OuterRef("proposal_id"),
        decision=ApprovalDecision.APPROVE,
        digest=OuterRef("digest"),
        proposal_revision=OuterRef("proposal_revision"),
        expires_at__gt=OuterRef("executed_at"),
    )
    receipts = (
        ActionExecution.objects.filter(
            client_id=cycle.client_id,
            cycle_id=cycle.pk,
            action_kind=ActionKind.REQUEST_WAIVER,
            proposal_id__in=proving(ActionKind.REQUEST_WAIVER)
            .filter(cycle_id=cycle.pk)
            .values("pk"),
        )
        .select_related("proposal")
        .annotate(proof_approved=Exists(approvals))
    )
    valid: set[UUID] = set()
    for receipt in receipts:
        if not receipt.proof_approved:
            continue
        proposal = receipt.proposal
        manifest = json.loads(bytes(proposal.manifest_bytes))
        spec = proposal.input_spec
        selected = spec.get("evidence_requests", []) if isinstance(spec, dict) else []
        for evidence in manifest["evidence"]:
            key = UUID(evidence["request_id"])
            row = candidates.get(key)
            if (
                row is not None
                and evidence["request_id"] in selected
                and evidence["revision"] == row[0] - 1
                and manifest["payload"]["reason"] == row[1]
            ):
                valid.add(key)
    return frozenset(valid)


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
    unmet = list(
        EvidenceRequest.objects.filter(cycle=cycle, required=True)
        .exclude(state=RequestState.ACCEPTED)
        .order_by("pk")
    )
    reasons = dict(
        RequestInternal.objects.filter(
            request__cycle=cycle, request__state=RequestState.WAIVED
        ).values_list("request_id", "waiver_reason")
    )
    proofs = waiver_proofs(
        cycle,
        {
            row.pk: (row.version, reasons.get(row.pk, ""))
            for row in unmet
            if row.state == RequestState.WAIVED
        },
    )
    for request in unmet:
        if request.pk in proofs:
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
        for proposal in with_marker_state(ActionProposal.objects.filter(cycle=cycle))
        .exclude(marker_state=MarkerState.EXACT)
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

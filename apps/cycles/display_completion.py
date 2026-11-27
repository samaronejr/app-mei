"""Display-only closure reads; completion_checks remains the locked authority."""

import json
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from django.db.models import Exists, OuterRef

from apps.cycles.completion_checks import CompletionItem
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionExecution,
    Approval,
    ApprovalDecision,
    ExceptionState,
    ProposalState,
    RequestState,
    TaskState,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations

    from apps.cycles.display_reads import CycleDetailReads
    from apps.cycles.display_requests import RequestFacts
    from apps.cycles.effects.manifest import EvidenceEffect
    from apps.cycles.models import EvidenceRequest, ServiceCycle


class ReceiptApproval(TypedDict):
    """An approval matching the exact receipt and its execution time."""

    display_approved: bool


class WaiverPayload(TypedDict):
    """The reason read from the already frozen, trusted waiver manifest."""

    reason: str


class WaiverManifest(TypedDict):
    """Only the frozen fields the authoritative waiver predicate consumes."""

    payload: WaiverPayload
    evidence: "list[EvidenceEffect]"


def display_valid_waivers(
    cycle: "ServiceCycle",
    requests: "Sequence[WithAnnotations[EvidenceRequest, RequestFacts]]",
) -> frozenset[UUID]:
    """Match batched receipts to the same reason, prior revision and approval."""
    candidates = {
        str(row.pk): row
        for row in requests
        if row.required and row.state == RequestState.WAIVED
    }
    if not candidates:
        return frozenset()
    approvals = Approval.objects.filter(
        proposal_id=OuterRef("proposal_id"),
        decision=ApprovalDecision.APPROVE,
        digest=OuterRef("digest"),
        proposal_revision=OuterRef("proposal_revision"),
        expires_at__gt=OuterRef("executed_at"),
    )
    receipts: QuerySet[WithAnnotations[ActionExecution, ReceiptApproval]] = (
        ActionExecution.objects.filter(
            client_id=cycle.client_id,
            cycle=cycle,
            action_kind=ActionKind.REQUEST_WAIVER,
        )
        .select_related("proposal")
        .annotate(display_approved=Exists(approvals))
    )
    valid: set[UUID] = set()
    for receipt in receipts:
        if not receipt.display_approved:
            continue
        manifest: WaiverManifest = json.loads(bytes(receipt.proposal.manifest_bytes))
        for evidence in manifest["evidence"]:
            row = candidates.get(evidence["request_id"])
            if (
                row is not None
                and evidence["request_id"]
                in receipt.proposal.input_spec["evidence_requests"]
                and evidence["revision"] == row.version - 1
                and _waiver_reason(row) == manifest["payload"]["reason"]
            ):
                valid.add(row.pk)
    return frozenset(valid)


def _waiver_reason(
    request: "WithAnnotations[EvidenceRequest, RequestFacts]",
) -> str | None:
    reason = request.display_waiver_reason
    return reason if reason.strip() else None


def display_completion_items(reads: "CycleDetailReads") -> list[CompletionItem]:
    """Keep every authoritative key, revision, label, link and category order."""
    cycle = reads.cycle
    base = f"/ciclos/{cycle.pk}/"
    items = [
        CompletionItem(
            f"tasks:{row.pk}:{row.version}",
            "tasks",
            "Tarefa ainda n\u00e3o aceita ou dispensada",
            base + "#graph-title",
        )
        for row in sorted(reads.tasks, key=lambda row: row.pk)
        if row.state not in {TaskState.ACCEPTED, TaskState.WAIVED}
    ]
    items.extend(
        CompletionItem(
            f"requests:{row.pk}:{row.version}",
            "requests",
            "Solicita\u00e7\u00e3o obrigat\u00f3ria sem aceite ou dispensa aprovada",
            base + f"#solicitacao-{row.pk}",
        )
        for row in sorted(reads.requests, key=lambda row: row.pk)
        if row.required
        and row.state != RequestState.ACCEPTED
        and row.pk not in reads.valid_waivers
    )
    items.extend(
        CompletionItem(
            f"exceptions:{row.pk}:{row.version}",
            "exceptions",
            "Pend\u00eancia aberta bloqueia a conclus\u00e3o",
            base + "#pendencias",
        )
        for row in sorted(reads.exceptions, key=lambda row: row.pk)
        if row.blocks_completion
        and row.state in {ExceptionState.OPEN, ExceptionState.HANDLING}
    )
    items.extend(_action_items(reads))
    if not reads.index_current:
        items.append(
            CompletionItem(
                f"evidence_index:{cycle.pk}:{cycle.effect_revision}",
                "evidence_index",
                "Gere um \u00edndice atualizado das evid\u00eancias",
                base + "evidencias/",
            )
        )
    return items


def _action_items(reads: "CycleDetailReads") -> list[CompletionItem]:
    items = [
        CompletionItem(
            f"actions:{row.pk}:{row.version}",
            "actions",
            "Efeito ainda n\u00e3o encerrado",
            f"/acoes/{row.pk}/",
        )
        for row in sorted(reads.proposals, key=lambda row: row.pk)
        if not row.display_executed
        and row.state
        not in {
            ProposalState.REJECTED,
            ProposalState.CANCELLED,
            ProposalState.INVALIDATED,
        }
    ]
    items.extend(
        CompletionItem(
            f"actions:{row['id']}:{row['version']}",
            "actions",
            "Comunica\u00e7\u00e3o sem resultado definido",
            "/comunicacao/",
        )
        for row in reads.unresolved_messages
    )
    return items

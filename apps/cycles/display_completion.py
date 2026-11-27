"""Display-only closure reads; completion_checks remains the locked authority."""

from typing import TYPE_CHECKING
from uuid import UUID

from apps.cycles.completion_checks import CompletionItem, waiver_proofs
from apps.cycles.effects.markers import MarkerState
from apps.cycles.models import (
    ExceptionState,
    ProposalState,
    RequestState,
    TaskState,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django_stubs_ext import WithAnnotations

    from apps.cycles.display_reads import CycleDetailReads
    from apps.cycles.display_requests import RequestFacts
    from apps.cycles.models import EvidenceRequest, ServiceCycle


def display_valid_waivers(
    cycle: "ServiceCycle",
    requests: "Sequence[WithAnnotations[EvidenceRequest, RequestFacts]]",
) -> frozenset[UUID]:
    """Return the locked waiver proof, fed the reasons this read cut annotated."""
    return waiver_proofs(
        cycle,
        {
            row.pk: (row.version, row.display_waiver_reason)
            for row in requests
            if row.required and row.state == RequestState.WAIVED
        },
    )


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
        if row.display_marker != MarkerState.EXACT
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

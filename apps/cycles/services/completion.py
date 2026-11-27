"""Justified closure and exact-effect amendment commands."""

import hashlib
from datetime import timedelta
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.cycles.completion_checks import completion_items, require_open_completion
from apps.cycles.completion_records import CompletionGeneration, CompletionJustification
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.errors import IllegalTransition
from apps.cycles.evidence_index import freeze_index
from apps.cycles.index_projection import digest_data
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    ApprovalDecision,
    CycleAmendment,
    CycleState,
    InternalNote,
    ProposalState,
    ServiceCycle,
)
from apps.cycles.services.cycle import check_version, emit, load_cycle, require_actor


def apply_completion(actor: User, proposal: ActionProposal) -> None:
    """Executor-only domain operation; its exact durable receipt is mandatory."""
    spec = ProposalInput.from_data(proposal.input_spec)
    cycle = load_cycle(actor, proposal.cycle_id, lock=True)
    require_actor(actor, "cycles.reopen" if spec.reopen else "cycles.complete")
    if not ActionExecution.objects.filter(
        proposal=proposal, digest=proposal.digest, proposal_revision=proposal.revision
    ).exists():
        msg = "Execute a proposta aprovada para alterar o encerramento."
        raise IllegalTransition(msg)
    if spec.reopen:
        _reopen(actor, proposal, cycle, spec)
        return
    if not require_open_completion(cycle, spec.completion_state):
        msg = "Esta transição não é permitida."
        if spec.completion_state == CycleState.COMPLETED:
            msg = (
                'Para concluir, o ciclo precisa estar em "Em execução". '
                'Volte ao ciclo e use "Próximo estado" para avançar por '
                '"Em revisão", "Aguardando aprovação" e "Em execução"; '
                "depois, execute a conclusão aprovada."
            )
        raise IllegalTransition(msg)
    items = completion_items(cycle)
    reasons = dict(spec.completion_reasons)
    if spec.completion_state == CycleState.COMPLETED and items:
        msg = "Há condições de conclusão pendentes."
        raise IllegalTransition(msg)
    if spec.completion_state == CycleState.WITH_PENDING and (
        set(reasons) != {item.key for item in items}
        or any(not reason.strip() for reason in reasons.values())
    ):
        msg = "Justifique cada pendência da revisão atual antes de encerrar."
        raise IllegalTransition(msg)
    previous = cycle.state
    cycle.state = spec.completion_state
    cycle.version += 1
    cycle.save(update_fields=["state", "version"])
    if spec.reason.strip():
        InternalNote.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            author=actor,
            body=spec.reason.strip(),
        )
    emit(
        cycle,
        actor,
        "cycle.transitioned",
        payload={"from": previous, "to": cycle.state},
    )


def freeze_completion(actor: User, proposal: ActionProposal) -> None:
    """Run after the execution event so the immutable cut includes its own proof."""
    spec = ProposalInput.from_data(proposal.input_spec)
    if spec.reopen:
        return
    cycle = ServiceCycle.objects.get(pk=proposal.cycle_id)
    index = freeze_index(actor, cycle)
    generation = CompletionGeneration.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        proposal=proposal,
        index=index,
        amendment=cycle.amendment,
        cycle_revision=cycle.version,
        snapshot=index.data,
        digest=index.digest,
    )
    CompletionJustification.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        generation=generation,
        reasons=dict(spec.completion_reasons),
    )


@transaction.atomic
def propose_reopen(
    actor: User,
    cycle_id: UUID,
    *,
    reason: str,
    affected_scope: tuple[UUID, ...],
    expected_version: int,
) -> ActionProposal:
    """Bind a reopening proposal to the preserved generation and affected scope."""
    from apps.cycles.services.approvals import propose  # noqa: PLC0415

    cycle = load_cycle(actor, cycle_id)
    check_version(cycle.version, expected_version)
    generation = CompletionGeneration.objects.filter(
        cycle=cycle, amendment=cycle.amendment
    ).first()
    if generation is None or not reason.strip() or not affected_scope:
        msg = "Informe a conclusão, o motivo e o escopo da emenda."
        raise ValidationError(msg)
    valid_ids = {cycle.pk}
    valid_ids.update(cycle.requests.values_list("pk", flat=True))
    valid_ids.update(cycle.tasks.values_list("pk", flat=True))
    if not set(affected_scope).issubset(valid_ids):
        msg = "O escopo da emenda deve pertencer a este ciclo."
        raise ValidationError(msg)
    step_key = f"reopen:{cycle.amendment}"
    previous = (
        ActionProposal.objects.filter(
            cycle=cycle,
            action_kind=ActionKind.CYCLE_COMPLETION,
            input_spec__step_key=step_key,
        )
        .order_by("-revision")
        .first()
    )
    return propose(
        actor,
        cycle.pk,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.CYCLE_COMPLETION,
                step_key=step_key,
                reason=reason,
                reopen=True,
                generation_id=generation.pk,
                affected_scope=tuple(sorted(affected_scope)),
            ),
            timezone.now() + timedelta(hours=72),
            previous_id=previous.pk if previous else None,
            previous_version=previous.version if previous else None,
        ),
        expected_version=expected_version,
    )


def _reopen(
    actor: User, proposal: ActionProposal, cycle: ServiceCycle, spec: ProposalInput
) -> None:
    if cycle.state not in {CycleState.COMPLETED, CycleState.WITH_PENDING}:
        msg = "Somente uma conclusão pode receber uma emenda."
        raise IllegalTransition(msg)
    generation_id = spec.generation_id
    if generation_id is None:
        msg = "A emenda precisa identificar a conclusão anterior."
        raise IllegalTransition(msg)
    generation = CompletionGeneration.objects.get(
        pk=generation_id, cycle=cycle, amendment=cycle.amendment
    )
    decision = (
        Approval.objects.filter(
            proposal=proposal, digest=proposal.digest, decision=ApprovalDecision.APPROVE
        )
        .exclude(approver_id=proposal.initiator_id)
        .first()
    )
    if decision is None:
        raise PermissionDenied
    cycle.amendment += 1
    CycleAmendment.objects.create(
        tenant_id=cycle.tenant_id,
        client_id=cycle.client_id,
        cycle=cycle,
        number=cycle.amendment,
        reason=spec.reason,
        pinned_playbook_version_id=cycle.playbook_version_id,
        affected_scope=[str(value) for value in spec.affected_scope],
        completed_snapshot=generation.snapshot,
        approved_by_id=decision.approver_id,
    )
    for pending in ActionProposal.objects.filter(
        cycle=cycle,
        executions__isnull=True,
        state__in=[ProposalState.AWAITING, ProposalState.APPROVED],
    ):
        pending.state = ProposalState.INVALIDATED
        pending.version += 1
        pending.invalidation_reasons = ["cycle_amended"]
        pending.save(update_fields=["state", "version", "invalidation_reasons"])
    cycle.state = CycleState.ACTIVE
    cycle.version += 1
    cycle.save(update_fields=["state", "version", "amendment"])
    emit(
        cycle,
        actor,
        "cycle.amended",
        payload={
            "generation_id": str(generation.pk),
            "number": cycle.amendment,
            "reason_digest": hashlib.sha256(spec.reason.encode()).hexdigest(),
            "scope_digest": digest_data([str(value) for value in spec.affected_scope]),
        },
    )

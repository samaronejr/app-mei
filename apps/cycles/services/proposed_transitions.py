"""Translate existing consequential form intents into exact, unexecuted proposals."""

from datetime import timedelta
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ActionProposal, CycleState, EvidenceRequest
from apps.cycles.services.approvals import propose
from apps.cycles.services.cycle import check_version, load_cycle


@transaction.atomic
def propose_waiver(
    actor: User, request_id: UUID, *, reason: str, expected_version: int
) -> ActionProposal:
    """Freeze a request under the proposal's authority-first lock order."""
    request = EvidenceRequest.objects.filter(pk=request_id).first()
    if request is None:
        raise Http404
    cycle = load_cycle(actor, request.cycle_id)
    check_version(request.version, expected_version)
    proposal = propose(
        actor,
        cycle.pk,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.REQUEST_WAIVER,
                step_key=f"waive:{request.pk}",
                evidence_request_ids=(request.pk,),
                reason=reason,
            ),
            timezone.now() + timedelta(hours=72),
        ),
        expected_version=cycle.version,
    )
    # propose locked the selected request before freezing it. A stale rendered
    # request version rolls back the whole proposal and its event.
    request.refresh_from_db()
    check_version(request.version, expected_version)
    return proposal


def propose_completion(
    actor: User,
    cycle_id: UUID,
    *,
    to_state: str,
    reason: str,
    expected_version: int,
) -> ActionProposal:
    """Propose a named terminal target; approval never overrides completion guards."""
    if to_state not in {CycleState.COMPLETED, CycleState.WITH_PENDING}:
        msg = "Escolha um resultado de encerramento."
        raise ValidationError(msg)
    return propose(
        actor,
        cycle_id,
        ProposalDraft(
            ProposalInput(
                action_kind=ActionKind.CYCLE_COMPLETION,
                step_key="completion",
                completion_state=to_state,
                reason=reason,
            ),
            timezone.now() + timedelta(hours=72),
        ),
        expected_version=expected_version,
    )

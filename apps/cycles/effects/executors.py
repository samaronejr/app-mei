"""Consequential effects: exact live revalidation and one durable business intent."""

import json
from dataclasses import dataclass
from typing import assert_never
from uuid import UUID

from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.maintenance import load_client
from apps.cycles.effects.build import initiation_capability
from apps.cycles.effects.delivery import message_intent
from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    ClientPublication,
    CycleEvent,
    EvidenceRequest,
    PublicationState,
    ServiceCycle,
)
from apps.cycles.services.approvals import _locked_proposal, revalidate
from apps.cycles.services.cycle import (
    check_version,
    require_actor,
    tenant_id,
    transition_cycle,
)
from apps.cycles.services.requests import waive_request
from apps.delivery.errors import IntentConflict
from apps.delivery.models import DeliveryEffect, OutboundMessage
from apps.delivery.policy import content_digest
from apps.delivery.services import record


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Queueing an email is a durable internal effect, never a delivery assertion."""

    proposal: ActionProposal
    executed: bool
    replayed: bool = False


def _existing(proposal: ActionProposal) -> bool:
    previous = ActionExecution.objects.filter(
        client_id=proposal.client_id, business_intent_key=proposal.business_intent_key
    ).first()
    if previous is not None and previous.digest != proposal.digest:
        msg = "A mesma intenção já tem efeitos diferentes registrados."
        raise IntentConflict(msg)
    return previous is not None


def _publish(proposal: ActionProposal) -> None:
    data = json.loads(bytes(proposal.manifest_bytes))
    payload = data["payload"]
    if "withdrawal" in payload:
        target = payload["withdrawal"]
        publication = ClientPublication.objects.select_for_update(no_key=True).get(
            pk=target["publication_id"], client_id=proposal.client_id
        )
        check_version(publication.version, target["version"])
        if publication.publication_digest != target["digest"]:
            msg = "A publicação não corresponde à versão aprovada."
            raise IntentConflict(msg)
        publication.state = PublicationState.WITHDRAWN
        publication.withdrawn_at = timezone.now()
        publication.version += 1
        publication.save(update_fields=["state", "withdrawn_at", "version"])
        event_type = "publication.withdrawn"
    else:
        publication = ClientPublication.objects.create(
            id=proposal.pk,
            tenant_id=proposal.tenant_id,
            client_id=proposal.client_id,
            cycle_id=proposal.cycle_id,
            proposal=proposal,
            proposal_revision=proposal.revision,
            title=payload["subject"],
            body_ptbr=payload["body"],
            documents=data["documents"],
            publication_digest=proposal.digest,
            business_intent_key=proposal.business_intent_key,
        )
        event_type = "publication.published"
    CycleEvent.objects.create(
        tenant_id=proposal.tenant_id,
        client_id=proposal.client_id,
        cycle_id=proposal.cycle_id,
        type=event_type,
        aggregate_type="ClientPublication",
        aggregate_id=publication.pk,
        aggregate_version=publication.version,
        payload={"proposal_id": str(proposal.pk), "digest": proposal.digest},
    )


def _queue(proposal: ActionProposal, actor: User) -> None:
    """Create new message rows only; never lock an existing message under a cycle."""
    data = json.loads(bytes(proposal.manifest_bytes))
    spec = ProposalInput.from_data(proposal.input_spec)
    cycle = proposal.cycle
    publication = spec.action_kind is ActionKind.CLIENT_PUBLICATION
    payload = data["payload"]["notice"] if publication else data["payload"]
    for recipient in data["recipients"]:
        if recipient["channel"] != "email":
            continue
        key = message_intent(proposal, UUID(recipient["contact_id"]))
        message = OutboundMessage(
            tenant_id=proposal.tenant_id,
            client_id=proposal.client_id,
            cycle=cycle,
            contact_id=UUID(recipient["contact_id"]),
            proposal_id=proposal.pk,
            created_by=actor,
            kind="approved_publication_notice" if publication else "approved_email",
            idempotency_key=key,
            business_intent_key=key,
            recipients_json=[recipient["address"]],
            subject=payload["subject"],
            body_ref=proposal.digest,
            portal_url=payload.get("url", ""),
        )
        message.content_digest = content_digest(message)
        previous = OutboundMessage.objects.filter(
            client_id=proposal.client_id, business_intent_key=key
        ).first()
        if previous is not None:
            if previous.content_digest != message.content_digest:
                msg = "A mesma intenção não pode enviar conteúdo diferente."
                raise IntentConflict(msg)
            continue
        effect = DeliveryEffect.objects.filter(
            client_id=proposal.client_id, business_intent_key=key
        ).first()
        if effect is not None:
            if effect.content_digest != message.content_digest:
                msg = "O registro de envio tem outro resumo de conteúdo."
                raise IntentConflict(msg)
            message.state = effect.outcome
        message.save(force_insert=True)
        record(message, message.state, actor=actor, code="enqueued")


@transaction.atomic
def execute(
    actor: User, proposal_id: UUID, *, expected_version: int
) -> ExecutionResult:
    """Revalidate all four effects under locks held until their durable writes."""
    candidate = ActionProposal.objects.filter(pk=proposal_id).first()
    if candidate is None:
        raise Http404
    load_client(actor, tenant_id(), candidate.client_id)
    if ActionExecution.objects.filter(
        client_id=candidate.client_id,
        business_intent_key=candidate.business_intent_key,
    ).exists():
        scope, proposal, spec, _decisions = _locked_proposal(
            actor, proposal_id, expected_version
        )
        scope.require(initiation_capability(spec.action_kind))
        return ExecutionResult(proposal, _existing(proposal), replayed=True)

    decision = revalidate(actor, proposal_id, expected_version=expected_version)
    proposal = decision.proposal
    spec = ProposalInput.from_data(proposal.input_spec)
    fresh_actor = User.objects.get(pk=actor.pk)
    require_actor(fresh_actor, initiation_capability(spec.action_kind))
    if not decision.executable:
        return ExecutionResult(proposal, executed=False)
    if _existing(proposal):
        return ExecutionResult(proposal, executed=True, replayed=True)
    ActionExecution.objects.create(
        tenant_id=proposal.tenant_id,
        client_id=proposal.client_id,
        cycle_id=proposal.cycle_id,
        proposal=proposal,
        proposal_revision=proposal.revision,
        digest=proposal.digest,
        business_intent_key=proposal.business_intent_key,
        action_kind=spec.action_kind,
        executed_by=fresh_actor,
    )
    match spec.action_kind:
        case ActionKind.CLIENT_PUBLICATION:
            _publish(proposal)
            _queue(proposal, fresh_actor)
        case ActionKind.EMAIL_NOTICE:
            _queue(proposal, fresh_actor)
        case ActionKind.REQUEST_WAIVER:
            request = EvidenceRequest.objects.get(pk=spec.evidence_request_ids[0])
            waive_request(
                fresh_actor,
                request.pk,
                reason=spec.reason,
                expected_version=request.version,
            )
        case ActionKind.CYCLE_COMPLETION:
            cycle = ServiceCycle.objects.get(pk=proposal.cycle_id)
            transition_cycle(
                fresh_actor,
                cycle.pk,
                spec.completion_state,
                expected_version=cycle.version,
                reason=spec.reason,
            )
        case unknown:
            assert_never(unknown)
    proposal.version += 1
    proposal.save(update_fields=["version"])
    CycleEvent.objects.create(
        tenant_id=proposal.tenant_id,
        client_id=proposal.client_id,
        cycle_id=proposal.cycle_id,
        actor=fresh_actor,
        type="proposal.queued"
        if spec.action_kind is ActionKind.EMAIL_NOTICE
        else "proposal.executed",
        aggregate_type="ActionProposal",
        aggregate_id=proposal.pk,
        aggregate_version=proposal.version,
        payload={"digest": proposal.digest, "intent": proposal.business_intent_key},
    )
    return ExecutionResult(proposal, executed=True)

"""The outbox's consequential-kind hooks, inside its dispatch-start transaction."""

import base64
import hashlib
import json
from datetime import timedelta
from uuid import UUID

from apps.cycles.effects.inputs import ProposalInput
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    ClientPublication,
    PublicationState,
)
from apps.cycles.services.approvals import revalidate
from apps.delivery.models import OutboundMessage
from apps.delivery.policy import BusinessIntent, MessageKind, register_kind

APPROVED_KINDS = frozenset({"approved_email", "approved_publication_notice"})


def message_intent(proposal: ActionProposal, contact_id: UUID) -> str:
    """One envelope per selected contact, without a regenerated proposal UUID."""
    spec = ProposalInput.from_data(proposal.input_spec)
    cycle = proposal.cycle
    return BusinessIntent(
        cycle.engagement_id,
        cycle.playbook_id,
        cycle.period_start,
        cycle.period_end + timedelta(days=1),
        cycle.amendment,
        f"{spec.step_key}:recipient:{contact_id}",
        str(spec.action_kind),
        spec.occurrence,
    ).key(proposal.tenant_id)


def _valid(message: OutboundMessage) -> tuple[bool, str]:
    if message.proposal_id is None:
        return False, "proposal_missing"
    result = revalidate(None, message.proposal_id)
    proposal = result.proposal
    if not result.executable:
        reason = next(
            (item.code for item in result.reasons if item.blocks_execution),
            "approval_required",
        )
        return False, reason
    if not ActionExecution.objects.filter(proposal=proposal).exists():
        return False, "execution_missing"
    data = json.loads(bytes(proposal.manifest_bytes))
    payload = data["payload"]
    if message.kind == "approved_publication_notice":
        target_id = payload.get("withdrawal", {}).get(
            "publication_id", str(proposal.pk)
        )
        state = (
            PublicationState.WITHDRAWN
            if "withdrawal" in payload
            else PublicationState.PUBLISHED
        )
        if not ClientPublication.objects.filter(pk=target_id, state=state).exists():
            return False, "publication_state_changed"
        payload = payload["notice"]
    recipients = [
        row["address"]
        for row in data["recipients"]
        if row["channel"] == "email" and row["contact_id"] == str(message.contact_id)
    ]
    valid = (
        recipients == message.recipients_json
        and len(recipients) == 1
        and message.subject == payload["subject"]
        and message.body_ref == proposal.digest
        and message.cycle_id == proposal.cycle_id
        and message.business_intent_key == message_intent(proposal, message.contact_id)
    )
    return valid, "" if valid else "effect_content_changed"


def _prepare(message: OutboundMessage) -> dict[str, str]:
    """Return immutable transport bytes; gateway consumes them after commit."""
    if message.proposal_id is None:
        code = "proposal_missing"
        raise ValueError(code)
    proposal = ActionProposal.objects.get(pk=message.proposal_id)
    payload = json.loads(bytes(proposal.manifest_bytes))["payload"]
    if message.kind == "approved_publication_notice":
        payload = payload["notice"]
    wire = base64.b64decode(payload["mime_base64"], validate=True)
    if hashlib.sha256(wire).hexdigest() != payload["mime_sha256"]:
        code = "frozen_mime_changed"
        raise ValueError(code)
    return {
        "frozen_mime": str(payload["mime_base64"]),
        "from": str(payload["from"]),
        "body": str(payload["body"]),
    }


def register() -> None:
    """Register only the two approved-content adapters, never caller templates."""
    for kind in sorted(APPROVED_KINDS):
        register_kind(kind, MessageKind("", "", _valid, _prepare))

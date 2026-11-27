"""The outbox's consequential-kind hooks, inside its dispatch-start transaction."""

import base64
import hashlib
import json
from datetime import timedelta
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import Http404

from apps.cycles.effects.manifest import ActionKind
from apps.cycles.effects.markers import MarkerState, marker_state
from apps.cycles.effects.stored import stored_input
from apps.cycles.errors import ProposalIntegrityError
from apps.cycles.models import (
    ActionProposal,
    ClientPublication,
    PublicationState,
)
from apps.cycles.services.approvals import _locked_proposal, revalidate
from apps.delivery.errors import IntentConflict
from apps.delivery.models import OutboundMessage
from apps.delivery.policy import BusinessIntent, MessageKind, register_kind
from apps.delivery.retries import root_intent

# Each approved envelope carries exactly one operation's effect.
OPERATIONS: dict[str, ActionKind] = {
    "approved_email": ActionKind.EMAIL_NOTICE,
    "approved_publication_notice": ActionKind.CLIENT_PUBLICATION,
}
APPROVED_KINDS = frozenset(OPERATIONS)
# Only an exact receipt authorizes a send (reviewer ruling 1).
STATE_REFUSALS: dict[MarkerState, str] = {
    MarkerState.CONFLICT: "execution_conflict",
    MarkerState.OCCUPIED: "execution_conflict",
    MarkerState.ABSENT: "execution_missing",
}


def message_intent(proposal: ActionProposal, contact_id: UUID) -> str:
    """One envelope per selected contact, without a regenerated proposal UUID."""
    spec = stored_input(proposal)
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
    """Known refusals become stable pre-start codes, so the outbox cancels the claim.

    A VersionConflict is transient and still escapes; nothing else is caught.
    """
    if message.proposal_id is None:
        return False, "proposal_missing"
    try:
        return _checked(message, message.proposal_id)
    except IntentConflict:
        code = "execution_conflict"
    except ProposalIntegrityError:
        code = "proposal_integrity"
    except Http404:
        code = "proposal_missing"
    except ValidationError as error:
        if getattr(error, "code", None) != "fiscal_manifestation_unavailable":
            raise
        code = "effect_unavailable"
    return False, code


def _checked(message: OutboundMessage, proposal_id: UUID) -> tuple[bool, str]:
    # Before revalidation (which may write an invalidation) and before any payload:
    # the envelope's operation against the stored, immutable row kind...
    stored = ActionProposal.objects.filter(pk=proposal_id).first()
    if stored is None:
        return False, "proposal_missing"
    if OPERATIONS.get(message.kind) != stored.action_kind:
        return False, "effect_kind_mismatch"
    # ...then, under the proposal fences, its stored identity and its marker state.
    _scope, locked, _spec, _decisions = _locked_proposal(None, proposal_id, None)
    refusal = STATE_REFUSALS.get(marker_state(locked)[0])
    if refusal is not None:
        return False, refusal
    result = revalidate(None, proposal_id)
    proposal = result.proposal
    if not result.executable:
        reason = next(
            (item.code for item in result.reasons if item.blocks_execution),
            "approval_required",
        )
        return False, reason
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
        and root_intent(message) == message_intent(proposal, message.contact_id)
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

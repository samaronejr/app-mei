"""Remove retained content copies while preserving their original proof digests."""

import json
from datetime import datetime
from uuid import UUID

from apps.cycles.effects.manifest import JSONValue, canonicalize
from apps.cycles.models import ActionProposal, ClientPublication

REDACTED = "[retention-redacted]"


def redact_payload(data: dict[str, JSONValue]) -> dict[str, JSONValue]:
    """Apply the closed content transform mirrored by the database guard."""
    result = dict(data)
    for key in ("subject", "body", "reason"):
        if key in result:
            result[key] = REDACTED
    if "mime_base64" in result:
        result["mime_base64"] = ""
    if "headers" in result:
        result["headers"] = {}
    notice = result.get("notice")
    if isinstance(notice, dict):
        result["notice"] = redact_payload(notice)
    reasons = result.get("completion_reasons")
    if isinstance(reasons, dict):
        result["completion_reasons"] = dict.fromkeys(reasons, REDACTED)
    return result


def redact_proposal(proposal_id: UUID, now: datetime) -> None:
    """Keep schema, identity, references, original digest and decisions readable."""
    proposal = ActionProposal.objects.select_for_update().get(pk=proposal_id)
    if proposal.redacted_at is not None:
        return
    manifest: dict[str, JSONValue] = json.loads(bytes(proposal.manifest_bytes))
    payload = manifest["payload"]
    if not isinstance(payload, dict):
        message = "Invalid persisted effect payload."
        raise TypeError(message)
    manifest["payload"] = redact_payload(payload)
    proposal.manifest_bytes = canonicalize(manifest)
    proposal.input_spec = redact_payload(proposal.input_spec)
    proposal.decision_reason = ""
    proposal.redacted_at = now
    proposal.save(
        update_fields=["manifest_bytes", "input_spec", "decision_reason", "redacted_at"]
    )


def redact_publication(publication_id: UUID, now: datetime) -> None:
    """Remove retained publication text while preserving its original digest."""
    ClientPublication.objects.filter(pk=publication_id, redacted_at=None).update(
        title=REDACTED, body_ptbr=REDACTED, redacted_at=now
    )

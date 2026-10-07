"""Deterministic, evidence-bound retry lineage without rewriting any effect."""

import hashlib

from apps.delivery.models import DeliveryEffect, MessageState, OutboundMessage


def retry_key(parent_intent: str) -> str:
    """Survive restores before the successor INSERT, just like the original key."""
    return hashlib.sha256(f"manual-retry:v1:{parent_intent}".encode()).hexdigest()


def root_intent(message: OutboundMessage) -> str:
    """Validate each retry link before using the original exact-approval identity."""
    current = message
    while current.retry_of is not None:
        parent = current.retry_of
        if (
            current.business_intent_key != retry_key(parent.business_intent_key)
            or current.content_digest != parent.content_digest
            or current.proposal_id != parent.proposal_id
            or current.kind != parent.kind
            or parent.state != MessageState.CANCELLED
            or parent.last_error_code != "staff_retry"
            or not DeliveryEffect.objects.filter(
                client_id=parent.client_id,
                business_intent_key=parent.business_intent_key,
                content_digest=parent.content_digest,
                outcome=MessageState.REJECTED,
            ).exists()
        ):
            return ""
        current = parent
    return str(current.business_intent_key)

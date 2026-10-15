"""Versioned manager decisions for definitively rejected envelopes."""

from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.approvals import lock_authorities
from apps.clients.maintenance import load_client
from apps.cycles.services.cycle import require_actor, require_enabled, tenant_id
from apps.delivery.errors import IntentConflict, VersionConflict
from apps.delivery.models import DeliveryEffect, MessageState, OutboundMessage
from apps.delivery.notice_authority import validate_notice
from apps.delivery.policy import still_valid
from apps.delivery.retries import retry_key
from apps.delivery.services import AUTOMATIONS_MANAGE, _control, record


def _locked_dead_letter(
    actor: User, message_id: UUID, expected_version: int, *, for_retry: bool = False
) -> tuple[User, OutboundMessage]:
    """Use the dispatch lock order: control, message, authority, client, sources."""
    require_enabled()
    _control()
    message = OutboundMessage.objects.select_for_update().filter(pk=message_id).first()
    if message is None:
        raise Http404
    if for_retry and message.kind not in {
        "approved_email",
        "approved_publication_notice",
    }:
        valid, reason = validate_notice(message, extra_actor=actor)
        if not valid:
            msg = f"O envio mudou e não pode ser repetido ({reason}). Revise a origem."
            raise ValidationError(
                msg,
                code=reason,
            )
        # PLATFORM_QUERY_OK: manager was included in the complete authority union.
        fresh_actor = User.objects.get(pk=actor.pk)
    else:
        authority = lock_authorities(
            {actor.pk},
            tenant_id=tenant_id(),
            extra_capabilities=(AUTOMATIONS_MANAGE,),
        )[actor.pk]
        fresh_actor = authority.user
    if not fresh_actor.is_active:
        raise PermissionDenied
    load_client(fresh_actor, tenant_id(), message.client_id, lock=True)
    require_actor(fresh_actor, AUTOMATIONS_MANAGE)
    if message.version != expected_version:
        msg = "Este registro mudou. Recarregue antes de salvar."
        raise VersionConflict(msg)
    receipt = DeliveryEffect.objects.filter(
        client_id=message.client_id, business_intent_key=message.business_intent_key
    ).first()
    if (
        message.state != MessageState.REJECTED
        or receipt is None
        or receipt.outcome != MessageState.REJECTED
        or receipt.content_digest != message.content_digest
        or OutboundMessage.objects.filter(retry_of=message).exists()
    ):
        msg = "Somente uma recusa conhecida, sem nova tentativa, permite esta ação."
        raise ValidationError(msg)
    return fresh_actor, message


def _close_rejection(message: OutboundMessage, actor: User, code: str) -> None:
    message.state = MessageState.CANCELLED
    message.last_error_code = code
    message.version += 1
    message.updated_at = timezone.now()
    message.save(update_fields=["state", "last_error_code", "version", "updated_at"])
    record(message, MessageState.CANCELLED, actor=actor, code=code)


@transaction.atomic
def retry_dead_letter(
    actor: User, message_id: UUID, *, expected_version: int
) -> OutboundMessage:
    """Queue one explicit successor; accepted/uncertain journal barriers still win."""
    fresh_actor, message = _locked_dead_letter(
        actor, message_id, expected_version, for_retry=True
    )
    valid, reason = still_valid(message)
    if not valid:
        msg = f"O envio mudou e não pode ser repetido ({reason}). Revise a origem."
        raise ValidationError(msg)
    key = retry_key(message.business_intent_key)
    receipt = DeliveryEffect.objects.filter(
        client_id=message.client_id, business_intent_key=key
    ).first()
    if receipt is not None and receipt.content_digest != message.content_digest:
        msg = "O registro recuperado não corresponde ao conteúdo desta tentativa."
        raise IntentConflict(msg)
    successor = OutboundMessage.objects.create(
        tenant_id=message.tenant_id,
        client_id=message.client_id,
        contact_id=message.contact_id,
        cycle_id=message.cycle_id,
        created_by=message.created_by,
        retry_of=message,
        invite_id=message.invite_id,
        evidence_request_id=message.evidence_request_id,
        proposal_id=message.proposal_id,
        kind=message.kind,
        idempotency_key=key,
        business_intent_key=key,
        content_digest=message.content_digest,
        notice_authorization_bytes=message.notice_authorization_bytes,
        notice_authorization_digest=message.notice_authorization_digest,
        recipients_json=message.recipients_json,
        subject=message.subject,
        body_ref=message.body_ref,
        portal_url=message.portal_url,
        state=receipt.outcome if receipt is not None else MessageState.QUEUED,
    )
    _close_rejection(message, fresh_actor, "staff_retry")
    record(successor, successor.state, actor=fresh_actor, code="enqueued")
    record_event(
        action="delivery.dead_letter_retried",
        actor=fresh_actor,
        obj=ObjectRef("OutboundMessage", str(message.pk)),
        metadata={"version": message.version, "retry_id": str(successor.pk)},
    )
    return successor


@transaction.atomic
def cancel_dead_letter(
    actor: User, message_id: UUID, *, expected_version: int
) -> OutboundMessage:
    """Close a known refusal without removing its evidence or making a send claim."""
    fresh_actor, message = _locked_dead_letter(actor, message_id, expected_version)
    _close_rejection(message, fresh_actor, "staff_cancel")
    record_event(
        action="delivery.dead_letter_cancelled",
        actor=fresh_actor,
        obj=ObjectRef("OutboundMessage", str(message.pk)),
        metadata={"version": message.version},
    )
    return message

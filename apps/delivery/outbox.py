"""Enqueue exact, authorized client effects without performing external I/O."""

import hashlib
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.accounts.models import User
from apps.clients.maintenance import load_client
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.services.cycle import (
    load_cycle,
    require_actor,
    require_enabled,
    tenant_id,
)
from apps.delivery.errors import IntentConflict, VersionConflict
from apps.delivery.models import DeliveryEffect, OutboundMessage
from apps.delivery.policy import (
    KINDS,
    BusinessIntent,
    content_digest,
    resolve_recipient,
)
from apps.delivery.services import record


def enqueue(  # noqa: PLR0913 - exact effect needs scope, intent, recipient and revision
    actor: User,
    client_id: UUID,
    contact_id: UUID | str,
    *,
    intent: BusinessIntent,
    expected_version: int,
    kind: str = "notice",
    cycle_id: UUID | None = None,
    idempotency_key: str = "",
) -> OutboundMessage:
    """Serialize intent creation on the authorized client; retries return one row."""
    require_enabled()
    with transaction.atomic():
        client = load_client(actor, tenant_id(), client_id, lock=True)
        require_actor(actor, CYCLES_OPERATE)
        contact = resolve_recipient(client.pk, contact_id)
        definition = KINDS.get(kind)
        if definition is None:
            msg = "Tipo de mensagem não registrado."
            raise ValidationError(msg)
        if cycle_id is not None:
            cycle = load_cycle(actor, cycle_id)
            if (
                cycle.client_id != client.pk
                or cycle.engagement_id != intent.engagement
                or cycle.playbook_id != intent.playbook
                or cycle.period_start != intent.period_start
                or cycle.period_end != intent.period_end
                or cycle.amendment != intent.amendment
            ):
                msg = "A intenção não corresponde ao ciclo."
                raise ValidationError(msg)
        key = intent.key(tenant_id())
        caller_key = (
            hashlib.sha256(idempotency_key.encode()).hexdigest()
            if idempotency_key
            else key
        )
        parts = urlsplit(settings.PLATFORM_URL)
        host = f"{client.tenant.slug}-portal.{parts.hostname}"
        if parts.port:
            host = f"{host}:{parts.port}"
        message = OutboundMessage(
            tenant_id=tenant_id(),
            client=client,
            contact=contact,
            cycle_id=cycle_id,
            created_by=actor,
            kind=kind,
            business_intent_key=key,
            idempotency_key=caller_key,
            recipients_json=[contact.email],
            subject=definition.subject,
            body_ref=definition.template,
            portal_url=urlunsplit((parts.scheme, host, "/", "", "")),
        )
        message.content_digest = content_digest(message)
        previous = OutboundMessage.objects.filter(
            Q(business_intent_key=key) | Q(idempotency_key=caller_key), client=client
        ).first()
        if previous is not None:
            if (
                previous.content_digest != message.content_digest
                or previous.business_intent_key != key
            ):
                msg = "The same intent cannot authorize different content."
                raise IntentConflict(msg)
            return previous
        if client.version != expected_version:
            msg = "Este cliente mudou. Recarregue antes de salvar."
            raise VersionConflict(msg)
        prior_effect = DeliveryEffect.objects.filter(
            client=client, business_intent_key=key
        ).first()
        if prior_effect is not None:
            if prior_effect.content_digest != message.content_digest:
                msg = "The effect digest differs from this intent."
                raise IntentConflict(msg)
            message.state = prior_effect.outcome
        message.save()
        client.version += 1
        client.save(update_fields=["version"])
        record(message, message.state, actor=actor, code="enqueued")
        return message

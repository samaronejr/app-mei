"""Firm outbox phases: durable claim, private egress, fenced result."""

import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import EmailMessage
from django.utils import timezone

from apps.accounts.invites import InviteNotDispatchableError
from apps.audit.services import ObjectRef, record_event
from apps.core.flags import tenant_has
from apps.core.tasks import PlatformTask, assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.delivery.gateway import smtp_send
from apps.delivery.models import AutomationPause, MessageState
from apps.delivery.privacy import delivery_secrets
from apps.tenants.account_state import read_only_reason
from apps.tenants.authority import lock_users
from apps.tenants.models import (
    FirmMessageKind,
    FirmMessageState,
    FirmOutboundMessage,
    Tenant,
)
from apps.tenants.notifications import PreparedNotice, prepare_invite, prepare_summary

LEASE = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class NoticeClaim:
    """Only ids and a fence cross database phases."""

    tenant_id: UUID
    message_id: UUID
    fence: int
    intent: str


def _claim(tenant_id: UUID, message_id: UUID) -> NoticeClaim | None:
    with tenant_context(tenant_id):
        if settings.DELIVERY_PAUSED or not tenant_has(tenant_id, "firm_settings_r1"):
            return None
        pause, _created = AutomationPause.objects.get_or_create(tenant_id=tenant_id)
        pause = AutomationPause.objects.select_for_update().get(pk=pause.pk)
        if pause.paused:
            return None
        message = FirmOutboundMessage.objects.select_for_update().get(pk=message_id)
        now = timezone.now()
        if message.state == FirmMessageState.SENDING and (
            message.lease_until is not None and message.lease_until <= now
        ):
            message.state = FirmMessageState.UNKNOWN
            message.last_error_code = "interrupted_attempt"
            message.finished_at = now
            message.version += 1
            message.save(
                update_fields=["state", "last_error_code", "finished_at", "version"]
            )
            record_event(
                action="tenant.notification_result",
                obj=ObjectRef("FirmOutboundMessage", str(message.pk)),
                metadata={
                    "state": message.state,
                    "code": message.last_error_code,
                    "fence": message.fence,
                },
            )
            return None
        if message.state != FirmMessageState.QUEUED:
            return None
        day = timezone.localdate(now)
        if pause.quota_day != day:
            pause.quota_day, pause.reserved_today = day, 0
        if pause.reserved_today >= settings.DELIVERY_DAILY_CAP:
            return None
        pause.reserved_today += 1
        pause.save(update_fields=["quota_day", "reserved_today"])
        message.state = FirmMessageState.SENDING
        message.fence += 1
        message.version += 1
        message.attempt_id = uuid4()
        message.lease_until = now + LEASE
        message.save(
            update_fields=["state", "fence", "version", "attempt_id", "lease_until"]
        )
        return NoticeClaim(
            tenant_id, message.pk, message.fence, message.idempotency_key
        )


def _prepare(claim: NoticeClaim) -> PreparedNotice | None:
    with tenant_context(claim.tenant_id):
        candidate = FirmOutboundMessage.objects.get(pk=claim.message_id)
        lock_users(
            pk for pk in (candidate.user_id, candidate.created_by_id) if pk is not None
        )
        message = (
            FirmOutboundMessage.objects.select_for_update(of=("self",))
            .select_related("user", "created_by")
            .get(pk=claim.message_id)
        )
        if (
            message.fence != claim.fence
            or message.state != FirmMessageState.SENDING
            or message.lease_until is None
            or message.lease_until <= timezone.now()
            or settings.DELIVERY_PAUSED
            or not tenant_has(claim.tenant_id, "firm_settings_r1")
            or AutomationPause.objects.filter(paused=True).exists()
        ):
            return None
        # PLATFORM_QUERY_OK: the delivery claim names exactly one authorized firm.
        tenant = Tenant.objects.get(pk=claim.tenant_id, is_active=True)
        if message.kind == FirmMessageKind.OWNER_INVITE:
            return prepare_invite(message)
        if read_only_reason(tenant):
            return None
        return prepare_summary(message, tenant, on=timezone.localdate())


def _finish(claim: NoticeClaim, state: str, code: str = "") -> None:
    with tenant_context(claim.tenant_id):
        message = FirmOutboundMessage.objects.select_for_update().get(
            pk=claim.message_id
        )
        if message.fence != claim.fence or message.state != FirmMessageState.SENDING:
            return
        message.state = state
        message.last_error_code = code
        message.finished_at = timezone.now()
        message.version += 1
        message.save(
            update_fields=["state", "last_error_code", "finished_at", "version"]
        )
        record_event(
            action="tenant.notification_result",
            obj=ObjectRef("FirmOutboundMessage", str(message.pk)),
            metadata={"state": state, "code": code, "fence": claim.fence},
        )


def dispatch_notice(tenant_id: UUID, message_id: UUID) -> None:
    """Never retry an uncertain effect, including after a database restore."""
    assert_no_transaction()
    claim = _claim(tenant_id, message_id)
    if claim is None:
        return
    prefix = f"firm-effects/{tenant_id}/{claim.intent}/"
    started_key = f"{prefix}started.json"
    started = False
    try:
        if default_storage.exists(started_key):
            _finish(claim, FirmMessageState.UNKNOWN, "prior_attempt")
            return
        prepared = _prepare(claim)
        if prepared is None:
            _finish(claim, FirmMessageState.CANCELLED, "authorization_changed")
            return
        receipt = {
            "tenant_id": str(tenant_id),
            "message_id": str(message_id),
            "intent": claim.intent,
            "digest": prepared.digest,
            "fence": claim.fence,
        }
        stored = default_storage.save(
            started_key, ContentFile(json.dumps(receipt).encode())
        )
        if stored != started_key:
            _finish(claim, FirmMessageState.UNKNOWN, "prior_attempt")
            return
        started = True
        with delivery_secrets(prepared.recipient, prepared.subject, prepared.body):
            reply = smtp_send(
                EmailMessage(
                    subject=prepared.subject,
                    body=prepared.body,
                    to=[prepared.recipient],
                    headers={"Message-ID": f"<{message_id}@firm-outbox.local>"},
                )
            )
        default_storage.save(
            f"{prefix}reply.json",
            ContentFile(
                json.dumps(
                    {**receipt, "outcome": reply.state, "code": reply.code}
                ).encode()
            ),
        )
        states: dict[str, str] = {
            MessageState.ACCEPTED: FirmMessageState.ACCEPTED,
            MessageState.UNKNOWN: FirmMessageState.UNKNOWN,
            MessageState.TEMPORARY: FirmMessageState.FAILED,
            MessageState.REJECTED: FirmMessageState.FAILED,
        }
        _finish(claim, states[reply.state], reply.code)
    except InviteNotDispatchableError:
        _finish(claim, FirmMessageState.CANCELLED, "invite_unavailable")
    except (OSError, BotoCoreError, ClientError):
        _finish(
            claim,
            FirmMessageState.UNKNOWN if started else FirmMessageState.FAILED,
            "storage_failed",
        )


def _dispatch_firm_outbox(self: PlatformTask) -> int:
    """Dispatch only enabled firms, with both global and per-firm pause controls."""
    assert_no_transaction()
    if settings.DELIVERY_PAUSED:
        return 0
    count = 0
    for tenant_id in self.tenant_ids():
        with tenant_context(tenant_id):
            if not tenant_has(tenant_id, "firm_settings_r1"):
                continue
            ids = list(
                FirmOutboundMessage.objects.filter(
                    state__in=[FirmMessageState.QUEUED, FirmMessageState.SENDING]
                ).values_list("pk", flat=True)[:50]
            )
        for message_id in ids:
            dispatch_notice(tenant_id, message_id)
            count += 1
    return count


dispatch_firm_outbox = shared_task(
    name="apps.tenants.notification_tasks.dispatch_firm_outbox",
    base=PlatformTask,
    bind=True,
    ignore_result=True,
)(_dispatch_firm_outbox)

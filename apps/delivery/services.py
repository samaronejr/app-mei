"""Versioned firm commands and the dispatcher's short, fenced DB phases."""

import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.approvals import lock_authorities
from apps.clients.maintenance import load_client
from apps.core.flags import tenant_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.models import CycleEvent, ServiceCycle
from apps.cycles.services.cycle import require_actor, require_enabled, tenant_id
from apps.cycles.services.exceptions import open_exception
from apps.delivery.contracts import Claim, Reply
from apps.delivery.errors import IntentConflict, StaleFenceError, VersionConflict
from apps.delivery.models import (
    AutomationPause,
    DeliveryAttempt,
    DeliveryEffect,
    MessageState,
    OutboundMessage,
)
from apps.delivery.policy import enabled_kinds

# S8.1: pausing and resuming automations is its own capability.
AUTOMATIONS_MANAGE = "automations.manage"
RETRY_MINUTES = (1, 5, 15, 60, 60)
MAX_ATTEMPTS = 6
CLAIM_LIMIT = 50
LEASE_SECONDS = 120
TRANSITIONS: dict[str, set[str]] = {
    MessageState.QUEUED: {MessageState.SENDING, MessageState.CANCELLED},
    MessageState.TEMPORARY: {MessageState.SENDING, MessageState.CANCELLED},
    MessageState.SENDING: {
        MessageState.ACCEPTED,
        MessageState.TEMPORARY,
        MessageState.REJECTED,
        MessageState.UNKNOWN,
        MessageState.CANCELLED,
    },
    MessageState.UNKNOWN: {MessageState.MANUAL_SENT, MessageState.MANUAL_NOT_SENT},
}


def record(
    message: OutboundMessage,
    outcome: str,
    *,
    code: str = "",
    actor: User | None = None,
    note: str = "",
) -> None:
    """Write a minimized receipt and domain envelope with the state transaction."""
    DeliveryAttempt.objects.create(
        tenant_id=message.tenant_id,
        client_id=message.client_id,
        message=message,
        attempt_id=message.lease_owner,
        fence=message.fence,
        outcome=outcome,
        error_code=code,
        actor_id=actor.pk if actor else None,
        note_digest=hashlib.sha256(note.encode()).hexdigest() if note else "",
    )
    if message.cycle_id:
        CycleEvent.objects.create(
            tenant_id=message.tenant_id,
            client_id=message.client_id,
            cycle_id=message.cycle_id,
            actor=actor,
            type="delivery.observed",
            aggregate_type="OutboundMessage",
            aggregate_id=message.pk,
            aggregate_version=message.version,
            payload={"state": outcome, "code": code},
        )


def transition(
    message: OutboundMessage, reply: Reply, *, actor: User | None = None, note: str = ""
) -> bool:
    """Record late observations without regressing terminal states."""
    allowed = reply.state in TRANSITIONS.get(message.state, set())
    if allowed:
        message.state = reply.state
        message.last_error_code = reply.code
        message.version += 1
        message.updated_at = timezone.now()
        if note:
            message.decision_note = note
        message.save()
    record(
        message,
        reply.state,
        code=reply.code if allowed else "late_observation",
        actor=actor,
        note=note,
    )
    return allowed


def effect(message: OutboundMessage) -> None:
    """Converge accepted receipts and reject conflicts even on replay."""
    existing, _ = DeliveryEffect.objects.get_or_create(
        client_id=message.client_id,
        business_intent_key=message.business_intent_key,
        defaults={
            "tenant_id": message.tenant_id,
            "message": message,
            "content_digest": message.content_digest,
            "outcome": message.state,
        },
    )
    if existing.content_digest != message.content_digest:
        msg = "The recorded effect has a different digest."
        raise IntentConflict(msg)


def report_problem(message: OutboundMessage) -> None:
    """Open a system-attributed cycle exception without impersonating a worker.

    A provider refusal first runs the kind's own terminal hook (T14: a refused
    portal invitation is withdrawn and gets its own exception).
    """
    from apps.delivery.policy import KINDS  # noqa: PLC0415

    kind = KINDS.get(message.kind)
    if (
        message.state == MessageState.REJECTED
        and kind is not None
        and kind.on_rejected is not None
    ):
        kind.on_rejected(message)
    if message.cycle_id is None:
        return
    cycle = ServiceCycle.objects.select_for_update().get(pk=message.cycle_id)
    open_exception(
        "delivery_unknown"
        if message.state == MessageState.UNKNOWN
        else "delivery_rejected",
        message.pk,
        actor=None,
        cycle_id=cycle.pk,
        expected_version=cycle.version,
        owner=cycle.owner or message.created_by,
        review_on=timezone.localdate(),
    )


def _control() -> AutomationPause:
    control, _ = AutomationPause.objects.get_or_create(tenant_id=tenant_id())
    return AutomationPause.objects.select_for_update().get(pk=control.pk)


def remaining_quota() -> int:
    """Reservations count attempts, including failures, to avoid exceeding quotas."""
    control = AutomationPause.objects.filter(tenant_id=tenant_id()).first()
    used = (
        control.reserved_today
        if control and control.quota_day == timezone.localdate()
        else 0
    )
    return max(0, int(settings.DELIVERY_DAILY_CAP) - used)


def claim_batch(tid: UUID) -> list[Claim]:
    """Commit SKIP LOCKED claims before the caller can perform any network I/O."""
    assert_no_transaction()
    with tenant_context(tid):
        kinds = enabled_kinds(tid)
        if not kinds:
            return []
        control = _control()
        now = timezone.now()
        # Expired sending leases are never reclaimed: the previous process might
        # still be transmitting. Only an explicit human decision can resolve them.
        expired = list(
            OutboundMessage.objects.select_for_update(skip_locked=True)
            .filter(kind__in=kinds, state=MessageState.SENDING, lease_until__lte=now)
            .order_by("pk")[:CLAIM_LIMIT]
        )
        paused = settings.DELIVERY_PAUSED or control.paused
        if not paused and control.quota_day != timezone.localdate():
            control.quota_day = timezone.localdate()
            control.reserved_today = 0
        limit = min(
            CLAIM_LIMIT, max(0, settings.DELIVERY_DAILY_CAP - control.reserved_today)
        )
        rows = (
            list(
                OutboundMessage.objects.select_for_update(skip_locked=True)
                .filter(
                    kind__in=kinds,
                    state__in=[MessageState.QUEUED, MessageState.TEMPORARY],
                    next_attempt_at__lte=now,
                    attempts__lt=MAX_ATTEMPTS,
                )
                .order_by("next_attempt_at", "pk")[:limit]
            )
            if not paused
            else []
        )
        # All existing messages precede source locks taken by problem reporting.
        for message in expired:
            transition(message, Reply(MessageState.UNKNOWN, "lease_expired"))
            effect(message)
            report_problem(message)
        if paused:
            return []
        claims = []
        for message in rows:
            existing = DeliveryEffect.objects.filter(
                client_id=message.client_id,
                business_intent_key=message.business_intent_key,
            ).first()
            if existing:
                if existing.content_digest != message.content_digest:
                    msg = "The effect digest changed."
                    raise IntentConflict(msg)
                message.state = existing.outcome
                message.version += 1
                message.save()
                record(message, existing.outcome, code="effect_replayed")
                continue
            message.fence += 1
            message.lease_owner = uuid4()
            message.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            message.send_started_at = None
            message.attempts += 1
            transition(message, Reply(MessageState.SENDING))
            claims.append(Claim(tid, message.pk, message.fence, message.lease_owner))
        control.reserved_today += len(claims)
        control.save()
        return claims


def _locked_claim(claim: Claim) -> OutboundMessage:
    message = OutboundMessage.objects.select_for_update().get(pk=claim.message_id)
    if (
        message.fence != claim.fence
        or message.lease_owner != claim.owner
        or message.state != MessageState.SENDING
        or message.lease_until is None
        or message.lease_until <= timezone.now()
    ):
        msg = "The delivery lease is no longer current."
        raise StaleFenceError(msg)
    return message


def authorize_and_prepare(claim: Claim) -> tuple[OutboundMessage, dict[str, str]]:
    """Reserve the single network send for this fence, after live re-evaluation.

    The gateway's only authorization entry point. It takes the message lock, refuses a
    fence that already entered the gateway, re-evaluates the message against the live
    domain rows (`still_valid`) and the pause/quota controls, and only then runs the
    kind's `prepare` hook.

    The hook runs in the same transaction and under the same message lock, after the
    live re-evaluation passed: a portal invitation mints its one-time token here
    (T11), so a revoked or accepted invitation is refused before a token exists. A
    hook refusing (`ValueError`) cancels the message with the hook's reason code. The
    returned values are never written to any row.
    """
    assert_no_transaction()
    with tenant_context(claim.tenant_id):
        return prepare_claim(claim)


@transaction.atomic
def prepare_claim(claim: Claim) -> tuple[OutboundMessage, dict[str, str]]:
    """Lock control, message, authority, client and cycle through dispatch start.

    The caller owns tenant_context. This is also the boundary exercised by real
    runtime-role race threads. Every acquired authorization lock survives through
    send_started_at and the outer commit; no I/O occurs here.
    """
    from apps.delivery.policy import KINDS, still_valid  # noqa: PLC0415

    if tenant_id() != claim.tenant_id:
        raise Http404
    control = _control()
    message = _locked_claim(claim)
    if message.send_started_at is not None:
        msg = "This fence already entered the gateway."
        raise StaleFenceError(msg)
    if (
        settings.DELIVERY_PAUSED
        or control.paused
        or message.kind not in enabled_kinds(claim.tenant_id)
    ):
        valid, reason = False, "delivery_paused"
    else:
        valid, reason = still_valid(message)
    secrets: dict[str, str] = {}
    prepare = KINDS[message.kind].prepare if valid else None
    if prepare is not None:
        try:
            secrets = prepare(message)
        except ValueError as refusal:
            valid, reason = False, str(refusal) or "prepare_refused"
    # Time keeps advancing while an authority/source acquisition is blocked.
    if message.lease_until is None or message.lease_until <= timezone.now():
        valid, reason = False, "lease_expired_before_dispatch"
    if not valid:
        transition(message, Reply(MessageState.CANCELLED, reason))
    else:
        message.send_started_at = timezone.now()
        message.save(update_fields=["send_started_at"])
        if message.proposal_id is not None and message.cycle_id is not None:
            CycleEvent.objects.create(
                tenant_id=message.tenant_id,
                client_id=message.client_id,
                cycle_id=message.cycle_id,
                type="proposal.dispatch_started",
                aggregate_type="ActionProposal",
                aggregate_id=message.proposal_id,
                aggregate_version=message.fence,
                payload={"message_id": str(message.pk), "digest": message.body_ref},
            )
    return message, secrets


def finalize(claim: Claim, reply: Reply) -> bool:
    """Finalize in a new transaction without regressing a newer fence or state."""
    assert_no_transaction()
    with tenant_context(claim.tenant_id):
        message = OutboundMessage.objects.select_for_update().get(pk=claim.message_id)
        if message.fence != claim.fence or message.lease_owner != claim.owner:
            DeliveryAttempt.objects.create(
                tenant_id=message.tenant_id,
                client_id=message.client_id,
                message=message,
                attempt_id=claim.owner,
                fence=claim.fence,
                outcome=reply.state,
                error_code="stale_fence",
            )
            return False
        if reply.state == MessageState.TEMPORARY:
            if message.attempts >= MAX_ATTEMPTS:
                reply = Reply(MessageState.REJECTED, "retry_exhausted")
            else:
                message.next_attempt_at = timezone.now() + timedelta(
                    minutes=RETRY_MINUTES[message.attempts - 1]
                )
        changed = transition(message, reply)
        if changed and reply.state == MessageState.ACCEPTED:
            effect(message)
        if changed and reply.state in {MessageState.UNKNOWN, MessageState.REJECTED}:
            effect(message)
            report_problem(message)
        return changed


def decide(
    actor: User, message_id: UUID, *, expected_version: int, action: str, note: str = ""
) -> OutboundMessage:
    """Explicit staff claims stay separate from server evidence; no blind retry."""
    require_enabled()
    with transaction.atomic():
        candidate = OutboundMessage.objects.filter(pk=message_id).first()
        if candidate is None:
            raise Http404
        message = OutboundMessage.objects.select_for_update().get(pk=message_id)
        load_client(actor, tenant_id(), message.client_id, lock=True)
        require_actor(actor, CYCLES_OPERATE)
        if message.version != expected_version:
            msg = "Este registro mudou. Recarregue antes de salvar."
            raise VersionConflict(msg)
        target = {
            "cancel": MessageState.CANCELLED,
            "sent": MessageState.MANUAL_SENT,
            "not_sent": MessageState.MANUAL_NOT_SENT,
        }.get(action)
        if target not in TRANSITIONS.get(message.state, set()) or (
            message.state == MessageState.SENDING
            or (action != "cancel" and not note.strip())
        ):
            msg = "Ação indisponível ou nota obrigatória."
            raise ValidationError(msg)
        transition(
            message,
            Reply(str(target), "staff_decision"),
            actor=actor,
            note=note.strip(),
        )
        return message


def set_pause(actor: User, *, paused: bool, expected_version: int) -> AutomationPause:
    """Change the kill switch; quota reservations do not change its revision."""
    if not (
        tenant_has(tenant_id(), "cycles_r1") or tenant_has(tenant_id(), "import_r1")
    ):
        raise Http404
    with transaction.atomic():
        control = _control()
        authority = lock_authorities(
            {actor.pk},
            tenant_id=tenant_id(),
            extra_capabilities=(AUTOMATIONS_MANAGE,),
        )[actor.pk]
        fresh_actor = authority.user
        if not fresh_actor.is_active:
            raise PermissionDenied
        require_actor(fresh_actor, AUTOMATIONS_MANAGE)
        if control.version != expected_version:
            msg = "Este registro mudou. Recarregue antes de salvar."
            raise VersionConflict(msg)
        control.paused = paused
        control.version += 1
        control.save()
        record_event(
            action="delivery.pause_changed",
            actor=fresh_actor,
            obj=ObjectRef("AutomationPause", str(control.pk)),
            metadata={"paused": paused, "version": control.version},
        )
        return control

"""One hold/eligibility decision and explicit, versioned hold commands."""

import calendar
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.clients.maintenance import load_client
from apps.cycles.errors import IllegalTransition
from apps.cycles.retention_records import ContentPurgeClaim, LegalHold
from apps.cycles.services.cycle import (
    check_version,
    require_actor,
    require_enabled,
    tenant_id,
)

MAX_HOLD_REASON = 2000


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """Unknown classification and active holds fail closed."""

    eligible: bool
    reason: str


def after_years(value: datetime, years: int) -> datetime:
    """Calendar years, including a conservative February leap-day boundary."""
    year = value.year + years
    return value.replace(
        year=year, day=min(value.day, calendar.monthrange(year, value.month)[1])
    )


def retention_decision(
    client_id: UUID, expires_at: datetime | None, *, now: datetime
) -> RetentionDecision:
    """Apply the active hold before interpreting a content expiration."""
    if LegalHold.objects.filter(client_id=client_id, released_at=None).exists():
        return RetentionDecision(eligible=False, reason="legal_hold")
    if expires_at is None:
        return RetentionDecision(eligible=False, reason="unknown_period")
    return RetentionDecision(
        expires_at <= now, "expired" if expires_at <= now else "retained"
    )


@transaction.atomic
def place_hold(
    actor: User, client_id: UUID, *, reason: str, expected_version: int
) -> LegalHold:
    """Client-wide protection; refuse if deletion already crossed its claim point."""
    require_enabled()
    client = load_client(actor, tenant_id(), client_id, lock=True)
    check_version(client.version, expected_version)
    require_actor(actor, "evidence.export")
    if not reason.strip() or len(reason) > MAX_HOLD_REASON:
        msg = "Informe um motivo de até 2.000 caracteres."
        raise ValidationError(msg)
    if ContentPurgeClaim.objects.filter(client=client, completed_at=None).exists():
        msg = "Há uma remoção de conteúdo em andamento. Resolva-a antes de reter."
        raise IllegalTransition(msg)
    hold = LegalHold.objects.create(
        tenant_id=tenant_id(), client=client, reason=reason.strip(), placed_by=actor
    )
    client.version += 1
    client.save(update_fields=["version"])
    record_event(
        action="retention.hold_placed",
        actor=actor,
        obj=ObjectRef("LegalHold", str(hold.pk)),
        metadata={"client_id": str(client.pk)},
    )
    return hold


@transaction.atomic
def release_hold(actor: User, hold_id: UUID, *, expected_version: int) -> LegalHold:
    """Release only the revision the staff member actually reviewed."""
    from django.utils import timezone  # noqa: PLC0415

    require_enabled()
    candidate = LegalHold.objects.filter(pk=hold_id).first()
    if candidate is None:
        raise Http404
    load_client(actor, tenant_id(), candidate.client_id, lock=True)
    hold = LegalHold.objects.select_for_update().get(pk=hold_id)
    check_version(hold.version, expected_version)
    require_actor(actor, "evidence.export")
    if hold.released_at is not None:
        msg = "Esta retenção já foi liberada."
        raise IllegalTransition(msg)
    hold.released_at = timezone.now()
    hold.released_by = actor
    hold.version += 1
    hold.save(update_fields=["released_at", "released_by", "version"])
    record_event(
        action="retention.hold_released",
        actor=actor,
        obj=ObjectRef("LegalHold", str(hold.pk)),
        metadata={"version": hold.version},
    )
    return hold

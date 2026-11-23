"""One hold/eligibility decision and explicit, versioned hold commands."""

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage, Storage, default_storage
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
from apps.obligations.models import (
    AcquisitionOrigin,
    Document,
    FiscalSituationReport,
)

MAX_HOLD_REASON = 2000
FISCAL_TIMEZONE: Final = ZoneInfo("America/Sao_Paulo")


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """Unknown classification and active holds fail closed."""

    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RetentionAnchor:
    """One verified reference that places a retained copy in a fiscal year.

    `basis` names where the date comes from (a linked cycle, an obligation, a report
    emission...), `on` is that date and `source_id` the row that holds it.
    """

    basis: str
    on: date
    source_id: UUID


SINGLE_COPY_STORAGES: Final = (FileSystemStorage,)


def single_copy_storage(storage: Storage | None = None) -> bool:
    """Tell whether a key delete removes the only stored copy (an allow-list).

    A versioned bucket keeps noncurrent versions that a client hold cannot
    protect, and an unrecognized backend may do the same: neither is supported
    for disposing of federal content.
    """
    chosen = default_storage if storage is None else storage
    return chosen.__class__ in SINGLE_COPY_STORAGES


def federal_content(document: Document) -> bool:
    """Tell whether SERPRO obtained these bytes or a SITFIS report holds them."""
    return (
        document.acquisition_origin == AcquisitionOrigin.FEDERAL_PROVIDER
        or FiscalSituationReport.objects.filter(document=document).exists()
    )


def ordered_anchors(anchors: Iterable[RetentionAnchor]) -> tuple[RetentionAnchor, ...]:
    """Distinct anchors in a stable order: date, then basis, then source."""
    return tuple(
        sorted(
            set(anchors),
            key=lambda anchor: (anchor.on, anchor.basis, str(anchor.source_id)),
        )
    )


def fiscal_expiry(anchors: Iterable[RetentionAnchor]) -> datetime | None:
    """January 1 of the latest anchored year + 6, in Sao Paulo; None without anchors.

    The latest reference wins, so bytes several records share are kept for the
    longest of their periods. The R1 period stays provisional until OG-08.
    """
    years = [anchor.on.year for anchor in anchors]
    return datetime(max(years) + 6, 1, 1, tzinfo=FISCAL_TIMEZONE) if years else None


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

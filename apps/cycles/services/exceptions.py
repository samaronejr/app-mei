"""Idempotent exception openers; later domains register deterministic reason codes."""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.models import User
from apps.cycles.capabilities import CYCLES_OPERATE
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import ServiceCycle
from apps.cycles.services.cycle import (
    check_version,
    emit,
    load_cycle,
    require_actor,
)
from apps.tenants.models import Membership


@dataclass(frozen=True, slots=True)
class ExceptionReason:
    """A registered reason owns its next action and completion policy."""

    next_action: str
    blocks_completion: bool = True
    client_visible: bool = False


_REASONS: dict[str, ExceptionReason] = {}


def register_exception_reason(code: str, reason: ExceptionReason) -> None:
    """Register once at app startup; duplicate codes are a programming error."""
    if code in _REASONS:
        raise ValueError(code)
    _REASONS[code] = reason


def open_exception(  # noqa: PLR0913 - the registered-opener contract carries scope and ownership
    reason_code: str,
    subject_ref: UUID,
    *,
    actor: User | None,
    cycle_id: UUID,
    expected_version: int,
    owner: User,
    due_on: date | None = None,
    review_on: date | None = None,
    evidence_refs: tuple[UUID, ...] = (),
) -> CycleException:
    """Converge retries on one active exception, without rewriting its history."""
    if reason_code not in _REASONS or (due_on is None and review_on is None):
        msg = "Motivo registrado e prazo ou data de revisão são obrigatórios."
        raise ValidationError(msg)
    reason = _REASONS[reason_code]
    with transaction.atomic():
        if actor is None:
            # Trusted deterministic openers (delivery/scanning) record failures
            # even after access/flags change or during restore replay.
            # RLS still scopes them; this path never authorizes a new effect.
            cycle = ServiceCycle.objects.select_for_update().get(pk=cycle_id)
        else:
            cycle = load_cycle(actor, cycle_id, lock=True)
            require_actor(actor, CYCLES_OPERATE)
        existing = CycleException.objects.filter(
            cycle=cycle,
            reason_code=reason_code,
            subject_ref=subject_ref,
            state__in=["aberta", "em_tratamento"],
        ).first()
        if existing is not None:
            return existing
        check_version(cycle.version, expected_version)
        # PLATFORM_QUERY_OK: scope is the locked, portfolio-authorized cycle's
        # tenant, and this is an owner roster validation, not member enumeration.
        if not Membership.objects.filter(
            tenant_id=cycle.tenant_id,
            user=owner,
            client__isnull=True,
            **({"is_active": True} if actor is not None else {}),
        ).exists():
            msg = "O responsável deve pertencer à equipe ativa."
            raise ValidationError(msg)
        problem = CycleException.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            cycle=cycle,
            reason_code=reason_code,
            subject_ref=subject_ref,
            owner=owner,
            next_action=reason.next_action,
            due_on=due_on,
            review_on=review_on,
            blocks_completion=reason.blocks_completion,
            client_visible=reason.client_visible,
            evidence_refs=[str(ref) for ref in evidence_refs],
        )
        cycle.version += 1
        cycle.save(update_fields=["version"])
        emit(
            cycle,
            actor,
            "exception.opened",
            payload={"exception_id": str(problem.pk), "reason_code": reason_code},
        )
        return problem

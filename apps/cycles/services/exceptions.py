"""Owned exceptions: idempotent openers and the locked, versioned triage commands.

Reasons are registered once at startup by the domain that owns them. Waiving an
exception ("aceitar como risco") is a policy decision, so it is allowed only for the
closed allow-list `WAIVABLE_REASONS` below. It is never allowed for an uncertain
external effect (`delivery_unknown`, the outbox's `resultado_incerto`) or for a security
code (scan unavailable, divergent document). Codes are opted in here. A new
registration is never waivable until this list names it.
"""

from dataclasses import dataclass
from datetime import date
from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from apps.accounts.models import User
from apps.authz.portfolio import visible_clients
from apps.cycles.capabilities import CYCLES_OPERATE, CYCLES_REVIEW
from apps.cycles.errors import IllegalTransition
from apps.cycles.models import Exception as CycleException
from apps.cycles.models import (
    ExceptionInternal,
    ExceptionState,
    InternalNote,
    ServiceCycle,
)
from apps.cycles.services.cycle import (
    check_version,
    emit,
    load_cycle,
    require_actor,
)
from apps.tenants.models import Membership


@dataclass(frozen=True, slots=True)
class ExceptionReason:
    """A registered reason owns its next action, completion and portal policy.

    `next_action` and `label` are firm-internal copy. Only `client_message`
    is ever rendered in the portal, and only when `client_visible` is set. Both
    are fixed registry text, never user input.
    """

    next_action: str
    blocks_completion: bool = True
    client_visible: bool = False
    label: str = ""
    client_message: str = ""

    def __post_init__(self) -> None:
        """Refuse a client-visible reason without the sentence the client reads."""
        if self.client_visible != bool(self.client_message):
            msg = "client_visible and client_message go together"
            raise ValueError(msg)


# The closed allow-list of policy-waivable reasons. Adding a code here is a
# reviewed product decision. Uncertain-effect and security codes never belong here.
WAIVABLE_REASONS: Final = frozenset(
    {
        "solicitacao_atrasada",
        "certificado_vencendo",
        "perfil_fiscal_desconhecido",
    }
)
ACTIVE_STATES: Final = (ExceptionState.OPEN, ExceptionState.HANDLING)
EXCEPTION_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    ExceptionState.OPEN: frozenset({ExceptionState.HANDLING}),
    ExceptionState.HANDLING: frozenset(
        {ExceptionState.RESOLVED, ExceptionState.RISK_ACCEPTED}
    ),
}
NOTE_REQUIRED: Final = frozenset(
    {ExceptionState.RESOLVED, ExceptionState.RISK_ACCEPTED}
)
NOTE_MAX_LENGTH: Final = 2000

_REASONS: dict[str, ExceptionReason] = {}


def register_exception_reason(code: str, reason: ExceptionReason) -> None:
    """Register once at app startup; duplicate codes are a programming error."""
    if code in _REASONS:
        raise ValueError(code)
    _REASONS[code] = reason


def reason_for(code: str) -> ExceptionReason | None:
    """Return the registered reason, for labels and action lists."""
    return _REASONS.get(code)


def is_waivable(code: str) -> bool:
    """Only codes named in the allow-list, and registered, may be accepted as risk."""
    return code in WAIVABLE_REASONS and code in _REASONS


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
            state__in=ACTIVE_STATES,
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
            due_on=due_on,
            review_on=review_on,
            blocks_completion=reason.blocks_completion,
            client_visible=reason.client_visible,
            client_message=reason.client_message,
        )
        ExceptionInternal.objects.create(
            tenant_id=cycle.tenant_id,
            client_id=cycle.client_id,
            exception=problem,
            next_action=reason.next_action,
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


def _locked(
    actor: User, exception_id: UUID, expected_version: int, capability: str
) -> tuple[ServiceCycle, CycleException]:
    """Scope, lock, version, then authority: every refusal precedes every write."""
    candidate = CycleException.objects.filter(pk=exception_id).first()
    if candidate is None:
        raise Http404
    cycle = load_cycle(actor, candidate.cycle_id, lock=True)
    problem = CycleException.objects.select_for_update().get(pk=candidate.pk)
    check_version(problem.version, expected_version)
    require_actor(actor, capability)
    return cycle, problem


def eligible_owner(cycle: ServiceCycle, user: User) -> bool:
    """Accept only an active firm member whose portfolio includes this client."""
    # PLATFORM_QUERY_OK: tenants_membership is a tenancy root; confined to the
    # locked cycle's firm and to the one user being named.
    member = Membership.objects.filter(
        tenant_id=cycle.tenant_id, user=user, client__isnull=True, is_active=True
    ).exists()
    return (
        member
        and visible_clients(user, tenant_id=cycle.tenant_id)
        .filter(pk=cycle.client_id)
        .exists()
    )


def assign_exception(
    actor: User, exception_id: UUID, owner_id: UUID, *, expected_version: int
) -> CycleException:
    """Hand an active exception to a firm member who can see the client."""
    with transaction.atomic():
        cycle, problem = _locked(actor, exception_id, expected_version, CYCLES_OPERATE)
        if problem.state not in ACTIVE_STATES:
            msg = "Esta pendência já foi encerrada."
            raise IllegalTransition(msg)
        owner = User.objects.filter(pk=owner_id).first()
        if owner is None or not eligible_owner(cycle, owner):
            msg = "Escolha alguém da equipe que atenda este cliente."
            raise ValidationError(msg)
        if owner.pk == problem.owner_id:
            return problem
        problem.owner = owner
        problem.version += 1
        problem.save(update_fields=["owner_id", "version"])
        emit(
            cycle,
            actor,
            "exception.assigned",
            aggregate=problem,
            payload={"owner_id": str(owner.pk)},
        )
        return problem


def transition_exception(
    actor: User,
    exception_id: UUID,
    to_state: str,
    *,
    expected_version: int,
    note: str = "",
) -> CycleException:
    """Triage, resolve or accept as risk, under S6.3's lock/version/can/event rule."""
    capability = (
        CYCLES_REVIEW if to_state == ExceptionState.RISK_ACCEPTED else CYCLES_OPERATE
    )
    note = note.strip()
    with transaction.atomic():
        cycle, problem = _locked(actor, exception_id, expected_version, capability)
        if to_state not in EXCEPTION_TRANSITIONS.get(problem.state, frozenset()):
            msg = "Esta pendência não pode fazer esta transição."
            raise IllegalTransition(msg)
        if to_state == ExceptionState.RISK_ACCEPTED and not is_waivable(
            problem.reason_code
        ):
            msg = (
                "Esta pendência não pode ser aceita como risco: "
                "ela precisa ser resolvida."
            )
            raise IllegalTransition(msg)
        if to_state in NOTE_REQUIRED and not note:
            msg = "Informe o que foi feito ou o motivo antes de encerrar."
            raise IllegalTransition(msg)
        if len(note) > NOTE_MAX_LENGTH:
            msg = "Use no máximo 2.000 caracteres."
            raise ValidationError(msg)
        previous = problem.state
        problem.state = to_state
        problem.version += 1
        problem.save(update_fields=["state", "version"])
        payload: dict[str, str | int] = {"from": previous, "to": to_state}
        if note:
            # The prose stays in the firm-only note table; the envelope carries ids.
            written = InternalNote.objects.create(
                tenant_id=cycle.tenant_id,
                client_id=cycle.client_id,
                cycle=cycle,
                author=actor,
                body=note,
            )
            payload["note_id"] = str(written.pk)
        emit(cycle, actor, "exception.transitioned", aggregate=problem, payload=payload)
        return problem

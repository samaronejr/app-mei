"""T14's deterministic openers, run by the daily sweep for flagged firms.

Every opener converges through `open_exception` on (cycle, reason_code, subject_ref)
under the cycle row lock, so re-running the sweep, or two sweeps racing, leaves
one active row per problem. Conditions are read from live rows at sweep time.

Owner resolution is a fixed chain: the cycle's owner, then the engagement's
responsible, then the firm's earliest active owner membership. A cycle with no
resolvable owner is counted and skipped: S6.3 requires every problem to have one.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import TaxRegime
from apps.clients.services import profile_at
from apps.cycles.evidence import EvidenceRequest, RequestState
from apps.cycles.models import CycleState, ServiceCycle
from apps.cycles.models import Exception as CycleException
from apps.cycles.services.exceptions import ExceptionReason, open_exception
from apps.tenants.models import Membership, Tenant, TenantRole

REQUEST_OVERDUE: Final = "solicitacao_atrasada"
CERTIFICATE_EXPIRING: Final = "certificado_vencendo"
TAX_PROFILE_UNKNOWN: Final = "perfil_fiscal_desconhecido"
NO_RESPONSIBLE: Final = "responsavel_nao_atribuido"

CERTIFICATE_WINDOW_DAYS: Final = 30
CLOSED_STATES: Final = frozenset(
    {CycleState.COMPLETED, CycleState.WITH_PENDING, CycleState.CANCELLED}
)
OPEN_REQUEST_STATES: Final = frozenset(
    {
        RequestState.REQUESTED,
        RequestState.RECEIVED_BY_TEAM,
        RequestState.IN_REVIEW,
        RequestState.REJECTED,
    }
)

REASONS: Final[dict[str, ExceptionReason]] = {
    REQUEST_OVERDUE: ExceptionReason(
        "Cobre o cliente ou ajuste o prazo da solicitação.",
        label="solicitação atrasada",
    ),
    CERTIFICATE_EXPIRING: ExceptionReason(
        "Avise o cliente e acompanhe a renovação do certificado digital.",
        label="certificado digital vencendo",
        client_visible=True,
        client_message=(
            "Seu certificado digital vence em breve. Providencie a renovação "
            "para não interromper os serviços."
        ),
    ),
    TAX_PROFILE_UNKNOWN: ExceptionReason(
        "Registre o regime tributário do cliente nesta competência.",
        label="regime tributário sem informação",
    ),
    NO_RESPONSIBLE: ExceptionReason(
        "Defina a pessoa responsável pelo serviço contratado.",
        label="serviço sem responsável",
    ),
}


@dataclass(frozen=True, slots=True)
class EvaluationCounts:
    """What one sweep considered, for the task's own receipt."""

    evaluated: int = 0
    skipped_no_owner: int = 0


def _fallback_owner(tenant_id: UUID) -> User | None:
    members = (
        # PLATFORM_QUERY_OK: tenants_membership is a tenancy root; this firm only.
        Membership.objects.filter(
            tenant_id=tenant_id, client__isnull=True, is_active=True
        )
        .select_related("user")
        .order_by("created_at", "pk")
    )
    row = members.filter(role=TenantRole.OWNER).first() or members.first()
    return row.user if row else None


def _open(  # noqa: PLR0913 - the opener contract: reason, scope, subject, owner, dates
    reason_code: str,
    cycle: ServiceCycle,
    subject_ref: UUID,
    owner: User,
    *,
    due_on: date | None = None,
    review_on: date | None = None,
) -> None:
    with transaction.atomic():
        current = ServiceCycle.objects.select_for_update().get(pk=cycle.pk)
        # A daily sweep must not undo a recorded resolution or accepted risk.
        # The cycle lock serializes this check with every exception command.
        if CycleException.objects.filter(
            cycle=current, reason_code=reason_code, subject_ref=subject_ref
        ).exists():
            return
        open_exception(
            reason_code,
            subject_ref,
            actor=None,
            cycle_id=current.pk,
            expected_version=current.version,
            owner=owner,
            due_on=due_on,
            review_on=review_on,
            evidence_refs=(subject_ref,),
        )


def evaluate_cycle(cycle: ServiceCycle, today: date) -> bool:
    """Apply every rule to one open cycle; False when no owner can be resolved."""
    owner = (
        cycle.owner or cycle.engagement.responsible or _fallback_owner(cycle.tenant_id)
    )
    if owner is None:
        return False
    overdue = EvidenceRequest.objects.filter(
        cycle=cycle, due_on__lt=today, state__in=OPEN_REQUEST_STATES
    ).order_by("pk")
    for request in overdue:
        _open(REQUEST_OVERDUE, cycle, request.pk, owner, review_on=today)
    client = cycle.client
    expires = client.certificate_expires_on
    if expires is not None and expires <= today + timedelta(
        days=CERTIFICATE_WINDOW_DAYS
    ):
        _open(CERTIFICATE_EXPIRING, cycle, client.pk, owner, due_on=expires)
    profile = profile_at(client, cycle.period_start)
    if profile is None or profile.regime == TaxRegime.UNKNOWN:
        _open(TAX_PROFILE_UNKNOWN, cycle, cycle.pk, owner, review_on=today)
    if cycle.engagement.responsible_id is None:
        _open(NO_RESPONSIBLE, cycle, cycle.engagement_id, owner, review_on=today)
    return True


def evaluate_tenant_exceptions(
    tenant: Tenant, *, today: date | None = None
) -> EvaluationCounts:
    """Run every deterministic opener over one firm's open cycles.

    Called inside `tenant_context` (the task names the firm); `today` is
    injectable so tests pin the deadline arithmetic.
    """
    on = today or timezone.localdate()
    evaluated = skipped = 0
    cycles = (
        ServiceCycle.objects.filter(tenant=tenant)
        .exclude(state__in=CLOSED_STATES)
        .select_related("client", "engagement__responsible", "owner")
        .order_by("pk")
    )
    for cycle in cycles:
        if evaluate_cycle(cycle, on):
            evaluated += 1
        else:
            skipped += 1
    return EvaluationCounts(evaluated=evaluated, skipped_no_owner=skipped)

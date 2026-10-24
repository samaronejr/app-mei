"""Client authority, provider-held certificate status and their owned work (AXRM-008).

`authority_for(client, operation)` is the single admission answer every provider claim
asks (todo 9): `usable`, `missing`, `expired` or `unknown`, always with a reason. It
fails closed: an operation outside the catalog is `unknown`, a service with no record
is `missing`, and capture additionally needs a current, matching, non-divergent
provider-held certificate on every active Focus link of the client.

Authority changes are append-only revisions (`AuthorityRecord`): the firm-side
commands lock the client row, compare the caller's rendered chain `version`, recheck
`fiscal.authority.manage` inside the lock and append one successor plus an audit
event, or write nothing. A human declaration is `ativa` but never "verified"; only a
named verifier sets `verified_by`. After a revocation, expiry or provider-detected
absence, a new declaration waits in `verificacao_pendente` until verified (A7.3).

Problems become owned work, never empty results: with an open cycle they open the
registered cycle exception; without one they open an `IntegrationIssue`. Expiry
states are derived from the injected `today`, so no sweep has to rewrite history.
Nothing here creates or changes a `SourceObservation`: resolving an access problem
never marks a missing fact as obtained.
"""

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final
from uuid import UUID, uuid5

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.objects import load_visible_client
from apps.authz.services import can
from apps.clients.models import ClientCompany, Engagement
from apps.core.flags import tenant_has
from apps.cycles.models import ServiceCycle
from apps.cycles.openers import CLOSED_STATES, fallback_owner, open_once, owner_for
from apps.cycles.services.exceptions import ExceptionReason
from apps.integrations.models import (
    ACTIVE_ISSUE_STATES,
    AUTHORITY_CATALOG,
    CODE_NOT_REQUIRED,
    AuthorityRecord,
    AuthorityService,
    AuthorityStatus,
    CaptureStream,
    ClientProviderLink,
    CredentialState,
    DisablementState,
    IntegrationIssue,
    IssueReason,
    IssueState,
    LinkStatus,
    Provider,
)
from apps.integrations.services import (
    LINK_TRANSITIONS,
    STALE_MESSAGE,
    IntegrationConflictError,
    InvalidTransitionError,
    LinkMachine,
)
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Tenant

MANAGE_CAPABILITY: Final = "fiscal.authority.manage"
WARNING_DAYS: Final = (30, 7, 1)
EXPIRY_WINDOW_DAYS: Final = WARNING_DAYS[0]
CAPTURE_FLAG: Final = "document_capture"
FEDERAL_FLAG: Final = "federal_checks"
# Stable subject ids for per-service problems: one per (client, service), so the
# sweep and the provider-denial path converge on the same cycle exception.
_SUBJECT_NAMESPACE: Final = UUID("01a167d0-8008-7000-8000-000000000008")


class Admission(StrEnum):
    """The four answers `authority_for` gives (plan todo 8 step 2)."""

    USABLE = auto()
    MISSING = auto()
    EXPIRED = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """Whether an operation may use this client's authority now, and why."""

    state: Admission
    reason: str
    record: AuthorityRecord | None = None

    @property
    def usable(self) -> bool:
        """True only for an affirmative answer."""
        return self.state is Admission.USABLE


@dataclass(frozen=True, slots=True)
class Requirement:
    """What an A6 operation needs from the client before it may run."""

    service: str | None
    needs_certificate: bool = False


# The implemented A6 operations. Anything absent -- the excluded SP-X/FC-X/FC-CONC
# ids, the firm-level SP-TERMO/SP-AUTH, a typo -- is `unknown` and never usable.
OPERATION_REQUIREMENTS: Final[dict[str, Requirement]] = {
    "SP-SIT-P": Requirement(AuthorityService.SITFIS),
    "SP-SIT-R": Requirement(AuthorityService.SITFIS),
    "SP-PAG": Requirement(AuthorityService.PAGTOWEB),
    "SP-COMP": Requirement(AuthorityService.PAGTOWEB),
    "SP-DAS": Requirement(AuthorityService.PGMEI),
    # Configuration through the firm's Focus Token Master (A6): no client authority.
    "FC-EMP": Requirement(None),
    "FC-NFE": Requirement(AuthorityService.CAPTURA_NFE, needs_certificate=True),
    "FC-NFS": Requirement(AuthorityService.CAPTURA_NFSE, needs_certificate=True),
    "FC-CIE": Requirement(AuthorityService.CIENCIA, needs_certificate=True),
}
STREAM_SERVICES: Final[dict[str, str]] = {
    CaptureStream.NFE_RECEBIDAS: AuthorityService.CAPTURA_NFE,
    CaptureStream.NFSEN_RECEBIDAS: AuthorityService.CAPTURA_NFSE,
}
FEDERAL_SERVICES: Final = (AuthorityService.SITFIS, AuthorityService.PAGTOWEB)

# Declaring is the first step everywhere; after any loss of authority the new
# declaration must be verified before it is usable (A7.3).
_NEEDS_REVERIFICATION: Final = frozenset(
    {
        AuthorityStatus.AUSENTE_DETECTADA,
        AuthorityStatus.VENCIDA,
        AuthorityStatus.REVOGADA,
        AuthorityStatus.ESCOPO_INSUFICIENTE,
    }
)
_MISSING_STATUSES: Final = frozenset(
    {
        AuthorityStatus.REVOGADA,
        AuthorityStatus.AUSENTE_DETECTADA,
        AuthorityStatus.ESCOPO_INSUFICIENTE,
    }
)

REASONS: Final[dict[str, ExceptionReason]] = {
    IssueReason.AUTORIZACAO_AUSENTE: ExceptionReason(
        "Registre a procuração ou a autorização do cliente e verifique-a antes da "
        "próxima consulta. Nenhuma consulta foi feita sem ela.",
        label="autorização ausente",
    ),
    IssueReason.CERTIFICADO_PROVEDOR_VENCENDO: ExceptionReason(
        "Avise o cliente e acompanhe a renovação do certificado cadastrado no "
        "provedor antes do vencimento.",
        label="certificado no provedor vencendo",
    ),
    IssueReason.CERTIFICADO_PROVEDOR_VENCIDO: ExceptionReason(
        "O certificado cadastrado no provedor venceu: a captura está parada até o "
        "cliente cadastrar um certificado válido.",
        label="certificado no provedor vencido",
    ),
    IssueReason.CREDENCIAL_DIVERGENTE: ExceptionReason(
        "O provedor informou um certificado de outro CNPJ ou uma troca inesperada. "
        "A captura está bloqueada até a equipe conferir o cadastro no provedor.",
        label="credencial divergente no provedor",
    ),
}


def authority_subject(client_id: UUID, service: str) -> UUID:
    """Return the stable subject of a per-service authority problem."""
    return uuid5(_SUBJECT_NAMESPACE, f"{client_id}:{service}")


def _today(today: dt.date | None) -> dt.date:
    return today or timezone.localdate()


def head_record(client_id: UUID, service: str) -> AuthorityRecord | None:
    """Return the current revision of one client's authority for one service."""
    return (
        AuthorityRecord.objects.filter(client_id=client_id, service=service)
        .select_related("verified_by", "declared_by", "evidence_document")
        .order_by("-version")
        .first()
    )


def evaluate_record(  # noqa: PLR0911 - one return per A7.3 outcome
    record: AuthorityRecord | None, today: dt.date
) -> AuthorityDecision:
    """Answer for one current revision on `today` (expiry is derived, not stored)."""
    if record is None:
        return AuthorityDecision(Admission.MISSING, "sem_registro")
    status = record.status
    if status in _MISSING_STATUSES:
        return AuthorityDecision(Admission.MISSING, status, record)
    if status == AuthorityStatus.VENCIDA:
        return AuthorityDecision(Admission.EXPIRED, status, record)
    if status not in {AuthorityStatus.ATIVA, AuthorityStatus.VENCENDO}:
        return AuthorityDecision(Admission.UNKNOWN, status, record)
    if record.valid_from is None or record.valid_from > today:
        return AuthorityDecision(Admission.UNKNOWN, "ainda_nao_vigente", record)
    if record.valid_to is None:
        return AuthorityDecision(Admission.USABLE, AuthorityStatus.ATIVA, record)
    if record.valid_to < today:
        return AuthorityDecision(Admission.EXPIRED, AuthorityStatus.VENCIDA, record)
    if (record.valid_to - today).days <= EXPIRY_WINDOW_DAYS:
        return AuthorityDecision(Admission.USABLE, AuthorityStatus.VENCENDO, record)
    return AuthorityDecision(Admission.USABLE, AuthorityStatus.ATIVA, record)


def validity_lapsed(record: AuthorityRecord, today: dt.date) -> bool:
    """Whether a revision's own validity ended before `today` (any status)."""
    return record.valid_to is not None and record.valid_to < today


def service_decision(
    client_id: UUID, service: str, *, today: dt.date | None = None
) -> AuthorityDecision:
    """Answer for one service, without the certificate check."""
    if AUTHORITY_CATALOG[service][1] == CODE_NOT_REQUIRED:
        return AuthorityDecision(Admission.USABLE, CODE_NOT_REQUIRED)
    return evaluate_record(head_record(client_id, service), _today(today))


def certificate_decision(
    client: ClientCompany, link: ClientProviderLink, today: dt.date
) -> AuthorityDecision:
    """Whether one link's provider-held certificate may be used for capture now."""
    state = link.credential_state
    if state == CredentialState.DIVERGENTE or (
        link.certificate_cnpj and link.certificate_cnpj != client.cnpj
    ):
        return AuthorityDecision(Admission.MISSING, "credencial_divergente")
    if state not in {CredentialState.ATIVA, CredentialState.VENCENDO}:
        return AuthorityDecision(Admission.MISSING, f"certificado_{state}")
    valid_to = link.certificate_valid_to
    if valid_to is None:
        return AuthorityDecision(Admission.UNKNOWN, "certificado_sem_validade")
    if valid_to < today:
        return AuthorityDecision(Admission.EXPIRED, "certificado_vencido")
    if link.certificate_valid_from is not None and link.certificate_valid_from > today:
        return AuthorityDecision(Admission.UNKNOWN, "certificado_ainda_nao_vigente")
    return AuthorityDecision(Admission.USABLE, "certificado_valido")


def _capture_links(client_id: UUID) -> list[ClientProviderLink]:
    return list(
        ClientProviderLink.objects.filter(
            client_id=client_id, connection__provider=Provider.FOCUS
        )
        .exclude(status=LinkStatus.REVOGADA)
        .order_by("pk")
    )


def authority_for(
    client: ClientCompany,
    operation: str,
    *,
    today: dt.date | None = None,
    link: ClientProviderLink | None = None,
) -> AuthorityDecision:
    """Answer whether `operation` may use `client`'s authority on `today`.

    Reads run under the caller's tenant context, so another firm's client has no
    visible record and is never usable. With `link`, only that binding's certificate
    is checked; without it, every non-revoked Focus link of the client must pass.
    """
    on = _today(today)
    requirement = OPERATION_REQUIREMENTS.get(operation)
    if requirement is None:
        return AuthorityDecision(Admission.UNKNOWN, "operacao_desconhecida")
    if requirement.service is None:
        decision = AuthorityDecision(Admission.USABLE, CODE_NOT_REQUIRED)
    else:
        decision = service_decision(client.pk, requirement.service, today=on)
    if not decision.usable or not requirement.needs_certificate:
        return decision
    links = [link] if link is not None else _capture_links(client.pk)
    if not links:
        return AuthorityDecision(Admission.MISSING, "sem_vinculo_provedor")
    for candidate in links:
        certificate = certificate_decision(client, candidate, on)
        if not certificate.usable:
            return certificate
    return decision


def client_owner(client: ClientCompany) -> User | None:
    """Return the engagement's responsible, else the firm fallback owner (or None)."""
    engagement = (
        Engagement.objects.filter(client_id=client.pk, responsible__isnull=False)
        .select_related("responsible")
        .order_by("starts_on", "pk")
        .first()
    )
    if engagement is not None and engagement.responsible is not None:
        return engagement.responsible
    return fallback_owner(client.tenant_id)


@dataclass(frozen=True, slots=True)
class Problem:
    """One owned provider problem, before it is routed to a cycle or an issue."""

    reason: str
    subject_ref: UUID
    review_on: dt.date
    service: str = ""
    link: ClientProviderLink | None = None
    due_on: dt.date | None = None


def raise_problem(client: ClientCompany, problem: Problem) -> None:
    """Open the problem once: as cycle exceptions, or as an integration issue.

    Every open cycle of the client gets the registered exception (same convergence
    rule as the R1 openers). A client without an open cycle gets one active
    `IntegrationIssue` per (reason, service); an existing one is left untouched.
    """
    cycles = (
        ServiceCycle.objects.filter(client_id=client.pk)
        .exclude(state__in=CLOSED_STATES)
        .select_related("engagement__responsible")
        .order_by("pk")
    )
    opened = False
    for cycle in cycles:
        owner = owner_for(cycle)
        if owner is None:
            continue
        open_once(
            problem.reason,
            cycle,
            problem.subject_ref,
            owner,
            due_on=problem.due_on,
            review_on=problem.review_on,
        )
        opened = True
    if opened:
        return
    if IntegrationIssue.objects.filter(
        client_id=client.pk,
        reason=problem.reason,
        service=problem.service,
        state__in=ACTIVE_ISSUE_STATES,
    ).exists():
        return
    owner = client_owner(client)
    if owner is None:
        return
    issue = IntegrationIssue.objects.create(
        tenant_id=client.tenant_id,
        client=client,
        reason=problem.reason,
        service=problem.service,
        link=problem.link,
        owner=owner,
        review_on=problem.review_on,
        due_on=problem.due_on,
        evidence_refs=[str(problem.subject_ref)],
    )
    record_event(
        action="integrations.issue_opened",
        tenant_id=client.tenant_id,
        obj=ObjectRef("IntegrationIssue", str(issue.pk)),
        metadata={"reason": problem.reason, "service": problem.service},
    )


def _resolve_issues(
    client_id: UUID,
    reasons: tuple[str, ...],
    *,
    service: str | None = None,
    link_id: UUID | None = None,
) -> None:
    """Close active issues whose condition no longer holds (cycle rows stay)."""
    issues = IntegrationIssue.objects.select_for_update().filter(
        client_id=client_id, reason__in=reasons, state__in=ACTIVE_ISSUE_STATES
    )
    if service is not None:
        issues = issues.filter(service=service)
    if link_id is not None:
        issues = issues.filter(link_id=link_id)
    for issue in issues.order_by("pk"):
        issue.state = IssueState.RESOLVIDA
        issue.version += 1
        issue.updated_at = timezone.now()
        issue.save(update_fields=["state", "version", "updated_at"])
        record_event(
            action="integrations.issue_resolved",
            tenant_id=issue.tenant_id,
            obj=ObjectRef("IntegrationIssue", str(issue.pk)),
            metadata={"reason": issue.reason, "service": issue.service},
        )


def _require(actor: User, client_id: UUID) -> None:
    if not can(actor, MANAGE_CAPABILITY, client_id):
        raise PermissionDenied


def _validate_service(service: str) -> None:
    if service not in AUTHORITY_CATALOG:
        message = "Serviço desconhecido."
        raise ValidationError(message, code="service")
    if AUTHORITY_CATALOG[service][1] == CODE_NOT_REQUIRED:
        message = "Este serviço não exige procuração no catálogo do SERPRO."
        raise ValidationError(message, code="nao_exigida")


def _validate_declaration(
    service: str,
    code: str,
    valid_from: dt.date,
    valid_to: dt.date | None,
    today: dt.date,
) -> None:
    _validate_service(service)
    if code != AUTHORITY_CATALOG[service][1]:
        message = "O código não corresponde ao serviço."
        raise ValidationError(message, code="code")
    if valid_to is not None and valid_to < valid_from:
        message = "A validade termina antes de começar."
        raise ValidationError(message, code="valid_to")
    if valid_to is not None and valid_to < today:
        message = "Esta autorização já venceu; registre a vigente."
        raise ValidationError(message, code="valid_to")


def _validate_evidence(document: Document | None, client: ClientCompany) -> None:
    if document is None:
        return
    if (
        document.client_id != client.pk
        or document.portal_visible
        or document.acquisition_origin != AcquisitionOrigin.UPLOAD_STAFF
    ):
        message = "A evidência deve ser um envio interno da equipe para este cliente."
        raise ValidationError(message, code="evidence_document")


def _locked_head(
    actor: User, client_id: UUID, service: str, expected_version: int
) -> tuple[ClientCompany, AuthorityRecord | None]:
    """Scope, lock, version, then authority -- every refusal precedes every write."""
    load_visible_client(actor, client_id)
    client = ClientCompany.objects.select_for_update().get(pk=client_id)
    head = head_record(client.pk, service)
    if (head.version if head else 0) != expected_version:
        raise IntegrationConflictError(STALE_MESSAGE)
    _require(actor, client.pk)
    return client, head


def _append(  # noqa: PLR0913 - one keyword per column of the new revision
    client: ClientCompany,
    head: AuthorityRecord | None,
    *,
    service: str,
    status: str,
    action: str,
    actor: User | None,
    **fields: object,
) -> AuthorityRecord:
    kind, code = AUTHORITY_CATALOG[service]
    record = AuthorityRecord.objects.create(
        tenant_id=client.tenant_id,
        client=client,
        kind=kind,
        service=service,
        code=code,
        status=status,
        supersedes=head,
        version=(head.version + 1) if head else 1,
        **fields,
    )
    record_event(
        action=action,
        tenant_id=client.tenant_id,
        actor=actor,
        obj=ObjectRef("AuthorityRecord", str(record.pk)),
        metadata={
            "service": service,
            "status": status,
            "version": record.version,
            "supersedes": str(head.pk) if head else "",
        },
    )
    return record


def declare_authority(  # noqa: PLR0913 - the rendered form's fields plus the clock
    actor: User,
    client_id: UUID,
    *,
    service: str,
    code: str,
    valid_from: dt.date,
    expected_version: int,
    valid_to: dt.date | None = None,
    evidence_document: Document | None = None,
    today: dt.date | None = None,
) -> AuthorityRecord:
    """Record a human declaration of authority; never a verification."""
    on = _today(today)
    visible = load_visible_client(actor, client_id)
    _require(actor, visible.pk)
    _validate_declaration(service, code, valid_from, valid_to, on)
    _validate_evidence(evidence_document, visible)
    with transaction.atomic():
        client, head = _locked_head(actor, client_id, service, expected_version)
        # A revision whose validity ended -- active or still pending verification --
        # can be declared again; otherwise a pending declaration that lapsed before
        # anyone verified it could never be verified, revoked or replaced.
        lapsed = head is not None and validity_lapsed(head, on)
        if head is None or head.status == AuthorityStatus.DESCONHECIDA:
            status = AuthorityStatus.ATIVA
        elif head.status in _NEEDS_REVERIFICATION or lapsed:
            status = AuthorityStatus.VERIFICACAO_PENDENTE
        else:
            message = (
                "Já existe uma autorização em andamento para este serviço: "
                "verifique-a ou revogue-a antes de registrar outra."
            )
            raise InvalidTransitionError(message)
        return _append(
            client,
            head,
            service=service,
            status=status,
            action="integrations.authority_declared",
            actor=actor,
            valid_from=valid_from,
            valid_to=valid_to,
            evidence_document=evidence_document,
            declared_by=actor,
        )


def verify_authority(
    actor: User,
    client_id: UUID,
    *,
    service: str,
    expected_version: int,
    today: dt.date | None = None,
) -> AuthorityRecord:
    """Let a named verifier confirm the current declaration (A7.3 -> ativa)."""
    on = _today(today)
    visible = load_visible_client(actor, client_id)
    _require(actor, visible.pk)
    _validate_service(service)
    with transaction.atomic():
        client, head = _locked_head(actor, client_id, service, expected_version)
        verifiable = head is not None and (
            head.status == AuthorityStatus.VERIFICACAO_PENDENTE
            or (head.status == AuthorityStatus.ATIVA and head.verified_by_id is None)
        )
        if head is None or not verifiable:
            message = "Não há declaração pendente de verificação para este serviço."
            raise InvalidTransitionError(message)
        if head.valid_to is not None and head.valid_to < on:
            message = "A autorização declarada já venceu; registre a vigente."
            raise InvalidTransitionError(message)
        record = _append(
            client,
            head,
            service=service,
            status=AuthorityStatus.ATIVA,
            action="integrations.authority_verified",
            actor=actor,
            valid_from=head.valid_from,
            valid_to=head.valid_to,
            evidence_document=head.evidence_document,
            declared_by=head.declared_by,
            verified_by=actor,
        )
        _resolve_issues(client.pk, (IssueReason.AUTORIZACAO_AUSENTE,), service=service)
        return record


def revoke_authority(
    actor: User,
    client_id: UUID,
    *,
    service: str,
    expected_version: int,
    today: dt.date | None = None,
) -> AuthorityRecord:
    """Record that the client withdrew the authority (ativa|vencendo -> revogada)."""
    on = _today(today)
    visible = load_visible_client(actor, client_id)
    _require(actor, visible.pk)
    _validate_service(service)
    with transaction.atomic():
        client, head = _locked_head(actor, client_id, service, expected_version)
        if head is None or not evaluate_record(head, on).usable:
            message = "Não há autorização vigente para revogar neste serviço."
            raise InvalidTransitionError(message)
        return _append(
            client,
            head,
            service=service,
            status=AuthorityStatus.REVOGADA,
            action="integrations.authority_revoked",
            actor=actor,
            valid_from=head.valid_from,
            valid_to=head.valid_to,
            evidence_document=head.evidence_document,
            declared_by=actor,
        )


def record_authority_denial(
    client: ClientCompany,
    operation: str,
    *,
    source_call_ref: str,
    http_status: int,
    today: dt.date | None = None,
) -> AuthorityRecord:
    """Append the absence a provider 403 proves, and open owned work.

    Called by the provider-call finalize path (todo 9) inside the firm's tenant
    context. Idempotent while the absence is already the current revision.
    """
    requirement = OPERATION_REQUIREMENTS.get(operation)
    service = requirement.service if requirement else None
    if service is None or AUTHORITY_CATALOG[service][1] == CODE_NOT_REQUIRED:
        message = f"{operation!r} não depende de autorização do cliente"
        raise ValueError(message)
    if http_status != 403:  # noqa: PLR2004 - the provider's "access denied" status
        message = f"only a 403 detects absent authority, got {http_status}"
        raise ValueError(message)
    if not source_call_ref:
        message = "source_call_ref is required"
        raise ValueError(message)
    on = _today(today)
    with transaction.atomic():
        locked = ClientCompany.objects.select_for_update().get(pk=client.pk)
        head = head_record(locked.pk, service)
        if head is not None and head.status == AuthorityStatus.AUSENTE_DETECTADA:
            return head
        record = _append(
            locked,
            head,
            service=service,
            status=AuthorityStatus.AUSENTE_DETECTADA,
            action="integrations.authority_absence_detected",
            actor=None,
            source_call_ref=source_call_ref,
        )
        raise_problem(
            locked,
            Problem(
                IssueReason.AUTORIZACAO_AUSENTE,
                authority_subject(locked.pk, service),
                review_on=on,
                service=service,
            ),
        )
        return record


def _next_warning(valid_to: dt.date, today: dt.date) -> dt.date:
    """Return the next 30/7/1-day warning date after today, else the expiry day."""
    for days in WARNING_DAYS:
        warning = valid_to - dt.timedelta(days=days)
        if warning > today:
            return warning
    return max(valid_to, today)


def certificate_problems(link: ClientProviderLink, today: dt.date) -> list[Problem]:
    """List the owned problems one link's certificate status implies on `today`."""
    if link.credential_state == CredentialState.DIVERGENTE:
        return [
            Problem(
                IssueReason.CREDENCIAL_DIVERGENTE, link.pk, review_on=today, link=link
            )
        ]
    valid_to = link.certificate_valid_to
    if link.credential_state == CredentialState.VENCIDA or (
        valid_to is not None and valid_to < today
    ):
        return [
            Problem(
                IssueReason.CERTIFICADO_PROVEDOR_VENCIDO,
                link.pk,
                review_on=today,
                link=link,
                due_on=valid_to,
            )
        ]
    if valid_to is not None and (valid_to - today).days <= EXPIRY_WINDOW_DAYS:
        return [
            Problem(
                IssueReason.CERTIFICADO_PROVEDOR_VENCENDO,
                link.pk,
                review_on=_next_warning(valid_to, today),
                link=link,
                due_on=valid_to,
            )
        ]
    return []


CERTIFICATE_REASONS: Final = (
    IssueReason.CERTIFICADO_PROVEDOR_VENCENDO,
    IssueReason.CERTIFICADO_PROVEDOR_VENCIDO,
    IssueReason.CREDENCIAL_DIVERGENTE,
)


def _reported_state(  # noqa: PLR0911, PLR0913 - one return per A7.3 outcome
    link: ClientProviderLink,
    client: ClientCompany,
    *,
    certificate_cnpj: str,
    valid_to: dt.date,
    generation: int,
    today: dt.date,
) -> tuple[str, int]:
    """Return the credential state and generation a provider report implies."""
    current = link.credential_state
    if certificate_cnpj != client.cnpj:
        return CredentialState.DIVERGENTE, link.credential_generation
    if generation != link.credential_generation:
        expected_change = (
            current
            in {
                CredentialState.AUSENTE,
                CredentialState.CONFIRMACAO_PENDENTE,
                CredentialState.ROTACAO_PENDENTE,
            }
            and generation == link.credential_generation + 1
        )
        if not expected_change:
            return CredentialState.DIVERGENTE, link.credential_generation
    elif current == CredentialState.ROTACAO_PENDENTE:
        return current, generation
    if current == CredentialState.AUSENTE:
        return CredentialState.CONFIRMACAO_PENDENTE, generation
    if current == CredentialState.DIVERGENTE:
        return current, link.credential_generation
    if valid_to < today:
        return CredentialState.VENCIDA, generation
    if (valid_to - today).days <= EXPIRY_WINDOW_DAYS:
        return CredentialState.VENCENDO, generation
    return CredentialState.ATIVA, generation


def record_certificate_status(  # noqa: PLR0913 - the provider's report plus the clock
    link_id: UUID,
    *,
    expected_version: int,
    certificate_cnpj: str,
    valid_from: dt.date | None,
    valid_to: dt.date,
    credential_generation: int,
    today: dt.date | None = None,
) -> ClientProviderLink:
    """Store what the provider reports about the client's certificate (todo 19 sync).

    Status only, never key material (INV-R7). A certificate of another CNPJ, or a
    generation change nobody asked for, is `divergente` and blocks capture; a move the
    A7.3 machine does not allow is treated the same way rather than accepted.
    """
    on = _today(today)
    with transaction.atomic():
        link = (
            ClientProviderLink.objects.select_for_update(of=("self",))
            .select_related("client")
            .get(pk=link_id)
        )
        if link.version != expected_version:
            raise IntegrationConflictError(STALE_MESSAGE)
        client = link.client
        current = link.credential_state
        target, generation = _reported_state(
            link,
            client,
            certificate_cnpj=certificate_cnpj,
            valid_to=valid_to,
            generation=credential_generation,
            today=on,
        )
        allowed = LINK_TRANSITIONS[LinkMachine.CREDENTIAL].get(current, frozenset())
        # Every state but `divergente` itself may move to `divergente`, so a move the
        # machine refuses always lands there instead of being accepted.
        if target != current and target not in allowed:
            target, generation = CredentialState.DIVERGENTE, link.credential_generation
        link.certificate_cnpj = certificate_cnpj
        link.certificate_valid_from = valid_from
        link.certificate_valid_to = valid_to
        link.certificate_synced_at = timezone.now()
        link.credential_state = target
        link.credential_generation = generation
        link.version += 1
        link.updated_at = timezone.now()
        link.save()
        record_event(
            action="integrations.certificate_status_recorded",
            tenant_id=link.tenant_id,
            obj=ObjectRef("ClientProviderLink", str(link.pk)),
            metadata={
                "from": current,
                "to": target,
                "valid_to": valid_to.isoformat(),
                "credential_generation": generation,
                "version": link.version,
            },
        )
        problems = certificate_problems(link, on)
        if not problems:
            _resolve_issues(client.pk, CERTIFICATE_REASONS, link_id=link.pk)
        for problem in problems:
            raise_problem(client, problem)
        return link


def evaluate_provider_conditions(tenant: Tenant, today: dt.date) -> None:
    """Open owned work for every enabled operation lacking usable authority.

    "Enabled" means an active, unpaused client link on a provider whose regional
    flag is effectively on: Focus links need the capture authorization of each
    configured stream and a usable certificate; SERPRO links need procuracoes
    00002 and 00004. Flags off leave every condition inert.
    """
    capture_on = tenant_has(tenant.pk, CAPTURE_FLAG)
    federal_on = tenant_has(tenant.pk, FEDERAL_FLAG)
    links = (
        ClientProviderLink.objects.filter(
            tenant=tenant,
            status=LinkStatus.ATIVA,
            disablement_state=DisablementState.ATIVA,
        )
        .select_related("client", "connection")
        .order_by("pk")
    )
    for link in links:
        provider = link.connection.provider
        if provider == Provider.FOCUS and capture_on:
            services = [STREAM_SERVICES[stream] for stream in link.streams]
            problems = certificate_problems(link, today)
        elif provider == Provider.SERPRO and federal_on:
            services = list(FEDERAL_SERVICES)
            problems = []
        else:
            continue
        for service in services:
            if not service_decision(link.client_id, service, today=today).usable:
                problems.append(
                    Problem(
                        IssueReason.AUTORIZACAO_AUSENTE,
                        authority_subject(link.client_id, service),
                        review_on=today,
                        service=service,
                    )
                )
        for problem in problems:
            raise_problem(link.client, problem)

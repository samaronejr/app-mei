"""The "Autorizações e certificados" section of the client workspace (plan A9).

Firm-side only (the portal never renders it) and drawn only while `federal_checks` or
`document_capture` is effectively on. Each row says what Axorim knows about one
service's authority and how it knows it: a declaration or a staff verification is a
person's record, never a provider confirmation, so nothing here uses the `confirmed`
badge (A7.7). Constant query count: one read for every current revision, one for
the client's Focus links, whatever the number of services.
"""

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final

from django.db.models import QuerySet
from django.utils import formats

from apps.accounts.models import User
from apps.authz.services import can
from apps.clients.models import ClientCompany
from apps.core.flags import tenant_has
from apps.integrations.authority import (
    CAPTURE_FLAG,
    FEDERAL_FLAG,
    MANAGE_CAPABILITY,
    Admission,
    certificate_decision,
    evaluate_record,
)
from apps.integrations.models import (
    AUTHORITY_CATALOG,
    CODE_NOT_REQUIRED,
    AuthorityRecord,
    AuthorityService,
    AuthorityStatus,
    ClientProviderLink,
    CredentialState,
    LinkStatus,
    Provider,
)


class Badge(StrEnum):
    """The partials/ui/status_badge.html states this section draws; never confirmed."""

    MANUAL = auto()
    PENDING = auto()
    UNKNOWN = auto()
    BLOCKED = auto()
    DANGER = auto()


FEDERAL_ROWS: Final = (
    AuthorityService.SITFIS,
    AuthorityService.PAGTOWEB,
    AuthorityService.PGMEI,
)
CAPTURE_ROWS: Final = (
    AuthorityService.CAPTURA_NFE,
    AuthorityService.CAPTURA_NFSE,
    AuthorityService.CIENCIA,
)
_DECLARABLE_AFTER: Final = frozenset(
    {
        AuthorityStatus.DESCONHECIDA,
        AuthorityStatus.AUSENTE_DETECTADA,
        AuthorityStatus.VENCIDA,
        AuthorityStatus.REVOGADA,
        AuthorityStatus.ESCOPO_INSUFICIENTE,
    }
)


@dataclass(frozen=True, slots=True)
class AuthorityRow:
    """One service as the section draws it."""

    service: str
    label: str
    code: str
    badge: str
    status_label: str
    detail: str
    record: AuthorityRecord | None
    version: int
    may_declare: bool
    may_verify: bool
    may_revoke: bool
    not_required: bool


@dataclass(frozen=True, slots=True)
class CertificateRow:
    """One Focus link's provider-held certificate, as the provider reported it."""

    link_id: str
    environment_label: str
    badge: str
    status_label: str
    detail: str


@dataclass(frozen=True, slots=True)
class AuthorityPanel:
    """The whole section: service rows, certificate rows, and whether to offer forms."""

    rows: list[AuthorityRow]
    certificates: list[CertificateRow]
    may_manage: bool


def _date(value: dt.date | dt.datetime | None) -> str:
    return formats.date_format(value, "d/m/Y") if value else ""


def _person(user: User | None) -> str:
    if user is None:
        return "provedor"
    return user.full_name or user.email


def _validity(record: AuthorityRecord) -> str:
    start = _date(record.valid_from)
    if record.valid_to is None:
        return f"Vigente desde {start}, sem data de término informada."
    return f"Vigente de {start} a {_date(record.valid_to)}."


_BLOCKED_LABELS: Final[dict[str, str]] = {
    AuthorityStatus.REVOGADA: "Revogada",
    AuthorityStatus.AUSENTE_DETECTADA: "Ausência detectada pelo provedor",
    AuthorityStatus.ESCOPO_INSUFICIENTE: "Escopo insuficiente",
}


def _blocked_detail(record: AuthorityRecord) -> str:
    when = _date(record.created_at)
    if record.status == AuthorityStatus.REVOGADA:
        return f"Revogação registrada por {_person(record.declared_by)} em {when}."
    if record.status == AuthorityStatus.AUSENTE_DETECTADA:
        return (
            f"O provedor recusou a consulta em {when}. Nenhuma nova consulta é "
            "feita até uma nova autorização ser verificada."
        )
    return "A autorização não cobre o serviço."


def _declared_label(record: AuthorityRecord) -> str:
    if record.verified_by is not None:
        return (
            f"Verificada por {_person(record.verified_by)} em "
            f"{_date(record.created_at)}"
        )
    if record.evidence_document_id is not None:
        return "Declarada (evidência anexada)"
    return "Declarada (sem evidência)"


def _status(  # noqa: PLR0911 - one return per displayed state
    record: AuthorityRecord | None, today: dt.date
) -> tuple[str, str, str]:
    """Return (badge state, label, detail) for one current revision."""
    if record is None:
        return Badge.UNKNOWN, "Sem registro", "Nenhuma autorização registrada."
    decision = evaluate_record(record, today)
    if record.status == AuthorityStatus.VERIFICACAO_PENDENTE:
        return Badge.PENDING, "Verificação pendente", _validity(record)
    if decision.state is Admission.EXPIRED:
        return Badge.DANGER, "Vencida", f"Venceu em {_date(record.valid_to)}."
    if record.status in _BLOCKED_LABELS:
        return Badge.BLOCKED, _BLOCKED_LABELS[record.status], _blocked_detail(record)
    if decision.state is Admission.UNKNOWN:
        return Badge.UNKNOWN, "Ainda não vigente", _validity(record)
    if decision.reason == AuthorityStatus.VENCENDO:
        return (
            Badge.PENDING,
            _declared_label(record),
            f"{_validity(record)} Vence em breve.",
        )
    return Badge.MANUAL, _declared_label(record), _validity(record)


def _row(
    service: str,
    record: AuthorityRecord | None,
    today: dt.date,
) -> AuthorityRow:
    code = AUTHORITY_CATALOG[service][1]
    label = AuthorityService(service).label
    if code == CODE_NOT_REQUIRED:
        return AuthorityRow(
            service=service,
            label=str(label),
            code=code,
            badge="",
            status_label="Não exigida",
            detail="O catálogo do SERPRO não lista procuração para este serviço.",
            record=None,
            version=0,
            may_declare=False,
            may_verify=False,
            may_revoke=False,
            not_required=True,
        )
    badge, status_label, detail = _status(record, today)
    decision = evaluate_record(record, today)
    declarable = (
        record is None
        or record.status in _DECLARABLE_AFTER
        or decision.state is Admission.EXPIRED
    )
    verifiable = record is not None and (
        record.status == AuthorityStatus.VERIFICACAO_PENDENTE
        or (record.status == AuthorityStatus.ATIVA and record.verified_by_id is None)
    )
    return AuthorityRow(
        service=service,
        label=str(label),
        code=code,
        badge=badge,
        status_label=status_label,
        detail=detail,
        record=record,
        version=record.version if record else 0,
        may_declare=declarable,
        may_verify=verifiable and decision.state is not Admission.EXPIRED,
        may_revoke=decision.usable,
        not_required=False,
    )


def _certificate(
    client: ClientCompany, link: ClientProviderLink, today: dt.date
) -> CertificateRow:
    decision = certificate_decision(client, link, today)
    badge: str
    reported = (
        f"Informado pelo provedor em {_date(link.certificate_synced_at)}."
        if link.certificate_synced_at
        else "O provedor ainda não informou o certificado."
    )
    valid_to = _date(link.certificate_valid_to)
    if decision.reason == "credencial_divergente":
        badge, label = Badge.BLOCKED, "Credencial divergente"
    elif decision.state is Admission.EXPIRED:
        badge, label = Badge.DANGER, f"Vencido em {valid_to}"
    elif decision.usable:
        within = link.credential_state == CredentialState.VENCENDO or (
            link.certificate_valid_to is not None
            and (link.certificate_valid_to - today).days <= 30  # noqa: PLR2004
        )
        badge = Badge.PENDING if within else ""
        label = f"Vence em {valid_to}" if within else f"Válido até {valid_to}"
    else:
        badge = Badge.UNKNOWN
        label = str(CredentialState(link.credential_state).label)
    return CertificateRow(
        link_id=str(link.pk),
        environment_label=link.connection.get_environment_display(),
        badge=badge,
        status_label=label,
        detail=reported,
    )


def _heads(
    client: ClientCompany, services: tuple[str, ...]
) -> QuerySet[AuthorityRecord]:
    return (
        AuthorityRecord.objects.filter(client_id=client.pk, service__in=services)
        .order_by("service", "-version")
        .distinct("service")
        .select_related("verified_by", "declared_by")
    )


def authority_panel(
    user: User, client: ClientCompany, today: dt.date
) -> AuthorityPanel | None:
    """Build the section for a client the caller already loaded through its portfolio.

    None while neither regional flag is on: the section is not drawn at all.
    """
    federal = tenant_has(client.tenant_id, FEDERAL_FLAG)
    capture = tenant_has(client.tenant_id, CAPTURE_FLAG)
    if not (federal or capture):
        return None
    services = (FEDERAL_ROWS if federal else ()) + (CAPTURE_ROWS if capture else ())
    heads = {record.service: record for record in _heads(client, services)}
    rows = [_row(service, heads.get(service), today) for service in services]
    certificates: list[CertificateRow] = []
    if capture:
        links = (
            ClientProviderLink.objects.filter(
                client_id=client.pk, connection__provider=Provider.FOCUS
            )
            .exclude(status=LinkStatus.REVOGADA)
            .select_related("connection")
            .order_by("pk")
        )
        certificates = [_certificate(client, link, today) for link in links]
    return AuthorityPanel(
        rows=rows,
        certificates=certificates,
        may_manage=can(user, MANAGE_CAPABILITY),
    )

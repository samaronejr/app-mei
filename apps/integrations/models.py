"""Provider connections and client provider links (plan A7.1, ADR-0008).

A `ProviderConnection` is one firm's configuration for one provider in one environment;
a `ClientProviderLink` binds one client company to it. Neither table holds a secret:
`secret_ref` is the *name* of a deployment secret, and certificate fields are status
the provider reports (CNPJ, validity, generation), never key material (INV-R7).

Tenant and client come only from these rows, never from a provider payload (INV-R1).
Both tables are tenant-isolated (`EnableRLS`), portal NONE (`DenyPortalAccess`,
roles.sql `PORTAL_NONE`) and the link's parents are composite (tenant, parent)
foreign keys added in `integrations.0001`.
"""

from typing import ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.integrations.call_models import (
    FirmProviderCall,
    FirmProviderCallAttempt,
    ProviderCall,
    ProviderCallAttempt,
    ProviderEffectBarrier,
    ProviderExecutionEpoch,
)
from apps.integrations.validators import (
    REF_MAX_LENGTH,
    REF_NAME_PATTERN,
    REF_VALUE_RUN_PATTERN,
    validate_secret_ref,
)

__all__ = [
    "ISSUE_SERVICE_LENGTH",
    "AuthorityKind",
    "AuthorityRecord",
    "AuthorityService",
    "AuthorityStatus",
    "CaptureStream",
    "ClientProviderLink",
    "ConnectionStatus",
    "CredentialState",
    "DisablementState",
    "FirmAuthorization",
    "FirmAuthorizationKind",
    "FirmAuthorizationStatus",
    "FirmProviderCall",
    "FirmProviderCallAttempt",
    "IntegrationIssue",
    "IssueReason",
    "IssueService",
    "IssueState",
    "LinkStatus",
    "Provider",
    "ProviderCall",
    "ProviderCallAttempt",
    "ProviderConnection",
    "ProviderEffectBarrier",
    "ProviderEnvironment",
    "ProviderExecutionEpoch",
    "ProviderOperationCode",
]


class Provider(models.TextChoices):
    """The two contracted providers (owner answer Q1, 2026-10-22)."""

    SERPRO = "serpro", "SERPRO (Integra Contador)"
    FOCUS = "focus", "Focus"


class ProviderEnvironment(models.TextChoices):
    """Where calls go. Only `producao` can ever verify anything (INV-R4)."""

    FAKE = "fake", "Simulado"
    TRIAL = "trial", "Teste do provedor"
    HOMOLOGACAO = "homologacao", "Homologação"
    PRODUCAO = "producao", "Produção"


class ConnectionStatus(models.TextChoices):
    """The firm-side state of a connection; closed by default."""

    INATIVA = "inativa", "Inativa"
    ATIVA = "ativa", "Ativa"
    PAUSADA = "pausada", "Pausada"
    CREDENCIAIS_AUSENTES = "credenciais_ausentes", "Credenciais ausentes"


class CaptureStream(models.TextChoices):
    """The received-document collections a link may read; another needs a new todo."""

    NFE_RECEBIDAS = "nfe_recebidas", "NF-e recebidas"
    NFSEN_RECEBIDAS = "nfsen_recebidas", "NFS-e Nacional recebidas"


class LinkStatus(models.TextChoices):
    """A7.1: configuration of the link itself."""

    CONFIGURACAO_PENDENTE = "configuracao_pendente", "Configuração pendente"
    ATIVA = "ativa", "Ativa"
    REVOGADA = "revogada", "Revogada"


class CredentialState(models.TextChoices):
    """A7.3: the provider-held certificate as the provider reports it."""

    AUSENTE = "ausente", "Ausente"
    CONFIRMACAO_PENDENTE = "confirmacao_pendente", "Confirmação pendente"
    ATIVA = "ativa", "Ativa"
    VENCENDO = "vencendo", "Vencendo"
    VENCIDA = "vencida", "Vencida"
    ROTACAO_PENDENTE = "rotacao_pendente", "Rotação pendente"
    DIVERGENTE = "divergente", "Divergente"


class DisablementState(models.TextChoices):
    """A7.3: local pause and provider-side disablement, read back before confirmed."""

    ATIVA = "ativa", "Ativa"
    PAUSA_SOLICITADA = "pausa_solicitada", "Pausa solicitada"
    PAUSADA_LOCALMENTE = "pausada_localmente", "Pausada localmente"
    DESATIVACAO_NO_PROVEDOR_PENDENTE = (
        "desativacao_no_provedor_pendente",
        "Desativação no provedor pendente",
    )
    DESATIVADA_NO_PROVEDOR_CONFIRMADA = (
        "desativada_no_provedor_confirmada",
        "Desativada no provedor (confirmada)",
    )


class ProviderConnection(TenantScopedModel):
    """One firm's connection to one provider in one environment.

    Spending caps live in the provider budget (todo 10), not here; `status` starts
    `inativa` and nothing in this todo can call a provider.
    """

    provider = models.CharField(max_length=16, choices=Provider.choices)
    environment = models.CharField(max_length=16, choices=ProviderEnvironment.choices)
    status = models.CharField(
        max_length=24,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.INATIVA,
    )
    secret_ref = models.CharField(
        max_length=REF_MAX_LENGTH,
        blank=True,
        default="",
        validators=[validate_secret_ref],
        help_text="Nome do segredo de implantação; nunca o valor.",
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "provider"], name="providerconnection_scope_idx"
            )
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "provider", "environment"),
                name="providerconnection_scope_uniq",
            ),
            # The unique anchor the link's (tenant_id, connection_id) composite FK
            # references; `id` alone is already unique.
            models.UniqueConstraint(
                fields=("tenant", "id"), name="providerconnection_tenant_id_uniq"
            ),
            models.CheckConstraint(
                condition=Q(secret_ref="")
                | (
                    Q(secret_ref__regex=REF_NAME_PATTERN)
                    & ~Q(secret_ref__regex=REF_VALUE_RUN_PATTERN)
                ),
                name="providerconnection_secret_ref_is_a_name",
            ),
        ]

    def __str__(self) -> str:
        """Name the provider and environment, never the secret reference."""
        return f"{self.get_provider_display()} ({self.get_environment_display()})"


class ClientProviderLink(TenantScopedModel):
    """One client company bound to one provider connection.

    Capture may run only when `status` is `ativa`, `credential_state` is `ativa` or
    `vencendo` and `disablement_state` is `ativa` (A7.3); the three are separate fields
    because they change for separate reasons. `capture_start_month` is the first day of
    the approved start month and never changes once set (INV-R11, enforced by a
    database trigger as well as the service).
    """

    client = models.ForeignKey(
        "clients.ClientCompany", on_delete=models.PROTECT, related_name="+"
    )
    connection = models.ForeignKey(
        ProviderConnection, on_delete=models.PROTECT, related_name="client_links"
    )
    provider_company_ref = models.CharField(max_length=64)
    streams = ArrayField(
        models.CharField(max_length=24, choices=CaptureStream.choices),
        default=list,
        blank=True,
    )
    capture_start_month = models.DateField(null=True, blank=True)
    certificate_cnpj = models.CharField(max_length=14, blank=True, default="")
    certificate_valid_from = models.DateField(null=True, blank=True)
    certificate_valid_to = models.DateField(null=True, blank=True)
    certificate_synced_at = models.DateTimeField(null=True, blank=True)
    credential_generation = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=24,
        choices=LinkStatus.choices,
        default=LinkStatus.CONFIGURACAO_PENDENTE,
    )
    credential_state = models.CharField(
        max_length=24,
        choices=CredentialState.choices,
        default=CredentialState.AUSENTE,
    )
    disablement_state = models.CharField(
        max_length=40,
        choices=DisablementState.choices,
        default=DisablementState.ATIVA,
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client"], name="clientproviderlink_scope_idx"
            )
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "client", "connection"),
                name="clientproviderlink_connection_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "id"),
                name="clientproviderlink_scope_id_uniq",
            ),
            # INV-R1: one provider company is never bound to two clients of the
            # same connection. Deliberately without client_id (exemption reasoned
            # in tests/isolation/test_client_scoped_uniqueness.py).
            models.UniqueConstraint(
                fields=("tenant", "connection", "provider_company_ref"),
                name="clientproviderlink_company_ref_uniq",
            ),
            models.CheckConstraint(
                condition=~Q(provider_company_ref=""),
                name="clientproviderlink_company_ref_present",
            ),
            models.CheckConstraint(
                condition=Q(
                    streams__contained_by=[choice.value for choice in CaptureStream]
                ),
                name="clientproviderlink_streams_known",
            ),
            models.CheckConstraint(
                condition=Q(capture_start_month__isnull=True)
                | Q(capture_start_month__day=1),
                name="clientproviderlink_capture_start_month",
            ),
        ]

    def __str__(self) -> str:
        """Name the link by its opaque provider reference."""
        return f"Vínculo {self.provider_company_ref}"


# --- AXRM-008: client authority, firm termo and owned provider problems ---------


class AuthorityKind(models.TextChoices):
    """What kind of permission a record documents (plan todo 8)."""

    PROCURACAO_ECAC = "procuracao_ecac", "Procuração eletrônica (e-CAC)"
    AUTORIZACAO_CAPTURA = "autorizacao_captura", "Autorização de captura"
    AUTORIZACAO_CIENCIA = "autorizacao_ciencia", "Autorização de ciência"


class AuthorityService(models.TextChoices):
    """The provider services that need (or are catalogued as not needing) authority."""

    SITFIS = "sitfis", "Situação fiscal (SITFIS)"
    PAGTOWEB = "pagtoweb", "Pagamentos (PAGTOWEB)"
    PGMEI = "pgmei", "Guia DAS do MEI (PGMEI)"
    CAPTURA_NFE = "captura_nfe", "Captura de NF-e recebidas"
    CAPTURA_NFSE = "captura_nfse", "Captura de NFS-e recebidas"
    CIENCIA = "ciencia", "Ciência da operação"


# The SERPRO procuracao catalog (A6) plus the client authorizations of capture and
# Ciencia: each service has exactly one kind and one code. `nao_exigida` is a catalog
# CODE (PGMEI lists no procuracao), never a status. Client authorizations have no
# e-CAC code; `autorizacao_cliente` names the signed client authorization instead.
CODE_NOT_REQUIRED: Final = "nao_exigida"
CLIENT_AUTHORIZATION_CODE: Final = "autorizacao_cliente"
AUTHORITY_CATALOG: Final[dict[str, tuple[str, str]]] = {
    AuthorityService.SITFIS: (AuthorityKind.PROCURACAO_ECAC, "00002"),
    AuthorityService.PAGTOWEB: (AuthorityKind.PROCURACAO_ECAC, "00004"),
    AuthorityService.PGMEI: (AuthorityKind.PROCURACAO_ECAC, CODE_NOT_REQUIRED),
    AuthorityService.CAPTURA_NFE: (
        AuthorityKind.AUTORIZACAO_CAPTURA,
        CLIENT_AUTHORIZATION_CODE,
    ),
    AuthorityService.CAPTURA_NFSE: (
        AuthorityKind.AUTORIZACAO_CAPTURA,
        CLIENT_AUTHORIZATION_CODE,
    ),
    AuthorityService.CIENCIA: (
        AuthorityKind.AUTORIZACAO_CIENCIA,
        CLIENT_AUTHORIZATION_CODE,
    ),
}


class AuthorityStatus(models.TextChoices):
    """A7.3 authority machine, per client and service."""

    DESCONHECIDA = "desconhecida", "Desconhecida"
    ATIVA = "ativa", "Ativa"
    VENCENDO = "vencendo", "Vencendo"
    VENCIDA = "vencida", "Vencida"
    REVOGADA = "revogada", "Revogada"
    AUSENTE_DETECTADA = "ausente_detectada", "Ausência detectada pelo provedor"
    ESCOPO_INSUFICIENTE = "escopo_insuficiente", "Escopo insuficiente"
    VERIFICACAO_PENDENTE = "verificacao_pendente", "Verificação pendente"


def _catalog_condition() -> Q:
    condition = Q(pk__in=[])
    for service, (kind, code) in AUTHORITY_CATALOG.items():
        condition |= Q(service=service, kind=kind, code=code)
    return condition


class AuthorityRecord(TenantScopedModel):
    """One immutable revision of one client's authority for one service.

    Append-only (trigger + runtime grants): a change is a successor row whose
    `supersedes` names the previous revision, exactly like `SourceObservation`. The
    chain is linear by construction -- `version` is the position in the chain, unique
    per (tenant, client, service), version 1 is exactly the root, and every row has at
    most one successor -- and the successor's composite foreign key repeats (tenant,
    client, service), so a chain can never cross a client or a service. The current
    revision is the highest `version`.

    A human declaration is never a verification: `verified_by` is set only by a
    named verifier, and a provider-detected absence carries the opaque
    `source_call_ref` of the refused call instead of a person.
    """

    client = models.ForeignKey(
        "clients.ClientCompany", on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=24, choices=AuthorityKind.choices)
    service = models.CharField(max_length=16, choices=AuthorityService.choices)
    code = models.CharField(max_length=24)
    status = models.CharField(max_length=24, choices=AuthorityStatus.choices)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    evidence_document = models.ForeignKey(
        "obligations.Document",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    declared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    source_call_ref = models.CharField(max_length=64, blank=True, default="")
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="successors",
        null=True,
        blank=True,
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "service", "version"],
                name="authorityrecord_head_idx",
            )
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "client", "id"),
                name="authorityrecord_scope_id_uniq",
            ),
            # The referent of the successor's (tenant, client, service, supersedes)
            # composite FK; `id` alone is already unique.
            models.UniqueConstraint(
                fields=("tenant", "client", "service", "id"),
                name="authorityrecord_chain_anchor_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "service", "version"),
                name="authorityrecord_service_version_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "supersedes"),
                condition=Q(supersedes__isnull=False),
                name="authorityrecord_one_successor_uniq",
            ),
            models.CheckConstraint(
                condition=_catalog_condition(),
                name="authorityrecord_catalog_code",
            ),
            models.CheckConstraint(
                condition=Q(supersedes__isnull=True, version=1)
                | Q(supersedes__isnull=False, version__gt=1),
                name="authorityrecord_version_follows_chain",
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True)
                | Q(valid_from__isnull=True)
                | Q(valid_to__gte=models.F("valid_from")),
                name="authorityrecord_valid_range",
            ),
            # Usable states need a validity start; a verification is always a
            # usable state; every row names a person or a provider call.
            models.CheckConstraint(
                condition=~Q(
                    status__in=[AuthorityStatus.ATIVA, AuthorityStatus.VENCENDO]
                )
                | Q(valid_from__isnull=False),
                name="authorityrecord_usable_has_start",
            ),
            models.CheckConstraint(
                condition=Q(verified_by__isnull=True) | Q(status=AuthorityStatus.ATIVA),
                name="authorityrecord_verified_is_active",
            ),
            models.CheckConstraint(
                condition=Q(declared_by__isnull=False) | ~Q(source_call_ref=""),
                name="authorityrecord_person_or_call",
            ),
        ]

    def __str__(self) -> str:
        """Name the service and revision, never the evidence."""
        return f"{self.get_service_display()} v{self.version}"


class FirmAuthorizationKind(models.TextChoices):
    """The firm-level authorizations (A6 SP-TERMO)."""

    TERMO_AUTORIZACAO_SERPRO = (
        "termo_autorizacao_serpro",
        "Termo de autorização (SERPRO)",
    )


class FirmAuthorizationStatus(models.TextChoices):
    """Outcome of one termo submission; a new submission is a new row."""

    ENVIADO = "enviado", "Enviado"
    ACEITO = "aceito", "Aceito"
    RECUSADO = "recusado", "Recusado"
    EXPIRADO = "expirado", "Expirado"


class FirmAuthorization(TenantScopedModel):
    """One firm-level authorization record, append-only (submitted by todo 13).

    Holds the signed document's reference and dates only: the procurador token the
    provider returns lives in the token broker's process memory, never here (INV-R7).
    """

    kind = models.CharField(max_length=32, choices=FirmAuthorizationKind.choices)
    signed_document = models.ForeignKey(
        "obligations.Document", on_delete=models.PROTECT, related_name="+"
    )
    valid_until = models.DateField(null=True, blank=True)
    submitted_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=FirmAuthorizationStatus.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "kind", "submitted_at"],
                name="firmauthorization_scope_idx",
            )
        ]
        # AXRM-009: the referent of the firm submission's tenant-paired FK.
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "id"), name="firmauthorization_tenant_id_uniq"
            ),
        ]

    def __str__(self) -> str:
        """Name the kind and outcome."""
        return f"{self.get_kind_display()} ({self.get_status_display()})"


class IssueReason(models.TextChoices):
    """Owned provider problems (plan todo 8 step 3); the same codes as the cycle's."""

    AUTORIZACAO_AUSENTE = "autorizacao_ausente", "Autorização ausente"
    CERTIFICADO_PROVEDOR_VENCENDO = (
        "certificado_provedor_vencendo",
        "Certificado no provedor vencendo",
    )
    CERTIFICADO_PROVEDOR_VENCIDO = (
        "certificado_provedor_vencido",
        "Certificado no provedor vencido",
    )
    CREDENCIAL_DIVERGENTE = "credencial_divergente", "Credencial divergente"
    # AXRM-009: an attempt whose provider outcome is unknown (A7.3 `incerta`).
    CHAMADA_PROVEDOR_INCERTA = (
        "chamada_provedor_incerta",
        "Chamada ao provedor com resultado incerto",
    )


class ProviderOperationCode(models.TextChoices):
    """Implemented A6 operations that may run as client provider calls (AXRM-009)."""

    SP_SIT_P = "SP-SIT-P", "SITFIS: solicitar protocolo"
    SP_SIT_R = "SP-SIT-R", "SITFIS: emitir relatório"
    SP_PAG = "SP-PAG", "PAGTOWEB: consultar pagamentos"
    SP_COMP = "SP-COMP", "PAGTOWEB: emitir comprovante"
    SP_DAS = "SP-DAS", "PGMEI: gerar DAS"
    FC_EMP = "FC-EMP", "Focus: empresa"
    FC_NFE = "FC-NFE", "Focus: NF-e recebidas"
    FC_CIE = "FC-CIE", "Focus: Ciência da operação"
    FC_NFS = "FC-NFS", "Focus: NFS-e recebidas"
    FC_PDF = "FC-PDF", "Focus: DANFe/DANFSe"


# An issue's service is an authority service (todo 8) or, for an uncertain
# provider call, the exact A6 operation, so SITFIS request and emission stay apart.
IssueService: Final = [*AuthorityService.choices, *ProviderOperationCode.choices]
# A client call's operation code is stored as its uncertainty issue's service.
ISSUE_SERVICE_LENGTH: Final = 16


class IssueState(models.TextChoices):
    """An owned problem is open, being handled, or resolved."""

    ABERTA = "aberta", "Aberta"
    EM_TRATAMENTO = "em_tratamento", "Em tratamento"
    RESOLVIDA = "resolvida", "Resolvida"


ACTIVE_ISSUE_STATES: Final = (IssueState.ABERTA, IssueState.EM_TRATAMENTO)


class IntegrationIssue(TenantScopedModel):
    """An authority or credential problem of a client without an open cycle.

    When the client has an open cycle the same condition opens the cycle exception
    instead; either way the problem has an owner and a date. At most one active row
    per (tenant, client, reason, service): the opener converges on it. `service` is
    blank for problems of the provider binding itself (certificate, credential),
    which `link` names.
    """

    client = models.ForeignKey(
        "clients.ClientCompany", on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=40, choices=IssueReason.choices)
    service = models.CharField(
        max_length=ISSUE_SERVICE_LENGTH, choices=IssueService, blank=True, default=""
    )
    link = models.ForeignKey(
        ClientProviderLink,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    state = models.CharField(
        max_length=16, choices=IssueState.choices, default=IssueState.ABERTA
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    review_on = models.DateField()
    due_on = models.DateField(null=True, blank=True)
    evidence_refs = ArrayField(
        models.CharField(max_length=64), default=list, blank=True
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "state"],
                name="integrationissue_scope_idx",
            )
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "client", "reason", "service"),
                condition=Q(state__in=ACTIVE_ISSUE_STATES),
                name="integrationissue_active_uniq",
            ),
        ]

    def __str__(self) -> str:
        """Name the reason and state."""
        return f"{self.get_reason_display()} ({self.get_state_display()})"

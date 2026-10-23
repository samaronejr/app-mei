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

from typing import ClassVar

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.integrations.validators import (
    REF_MAX_LENGTH,
    REF_NAME_PATTERN,
    REF_VALUE_RUN_PATTERN,
    validate_secret_ref,
)


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

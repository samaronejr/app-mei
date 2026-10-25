"""Federal consultation receipts, payment documents and fiscal-situation reports.

AXRM-014 (plan todo 14, A7.1, A7.3). Three client-scoped, firm-only tables
(DenyPortalAccess, INV-R10) that give SERPRO results a durable home before any
provider slice writes them:

* `FederalConsultation` is the RECEIPT of one consultation: what was asked (kind and
  range), when, in which provider environment, and what it ended up meaning. "No
  payment found" is a receipt with outcome `nada_localizado` -- never a payment
  observation, because absence in one source on one day proves nothing about the
  obligation.
* `FederalPayment` is one arrecadacao document the Receita returned. Append-only
  (trigger for every role, SELECT/INSERT only for app_runtime): a payment the
  official source reported is a historical fact, and a second consultation that sees
  the same `numero_documento` must agree with it, never rewrite it.
* `FiscalSituationReport` is the SITFIS protocol and emission lifecycle (A7.3). It is
  an as-of report and never "proves the month".

Only a matched payment from a `producao` call (with the owner's real-data approval)
becomes an official `SourceObservation`; that rule lives in `apps.obligations.federal`
(INV-R4). Every parent is a composite (tenant, client, parent) foreign key in the
migration, so a row can never name another client's consultation, obligation or
document.
"""

from typing import ClassVar

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TenantScopedModel

MAX_EMISSION_ATTEMPTS = 3


class FederalConsultationKind(models.TextChoices):
    """Which federal question the consultation asked."""

    PAGAMENTOS = "pagamentos", "Pagamentos (PAGTOWEB)"
    SITUACAO_FISCAL = "situacao_fiscal", "Situação fiscal (SITFIS)"


class FederalConsultationOutcome(models.TextChoices):
    """What the consultation ended up meaning; a receipt, never a payment claim."""

    ENCONTRADO = "encontrado", "Encontrado"
    NADA_LOCALIZADO = "nada_localizado", "Nada localizado"
    SEM_PROCURACAO = "sem_procuracao", "Sem procuração"
    FALHA = "falha", "Falha"
    COTA_ESGOTADA = "cota_esgotada", "Cota esgotada"
    INCERTO = "incerto", "Incerto"


class FederalEnvironment(models.TextChoices):
    """Where the call went. Mirrors `integrations.ProviderEnvironment` (pinned by test).

    Kept local so the obligations schema never imports the provider app; only
    `producao` can ever produce an official observation (INV-R4).
    """

    FAKE = "fake", "Simulado"
    TRIAL = "trial", "Demonstração (trial)"
    HOMOLOGACAO = "homologacao", "Homologação"
    PRODUCAO = "producao", "Produção"


class FederalPaymentType(models.TextChoices):
    """The arrecadacao document families PAGTOWEB returns."""

    DARF = "DARF", "DARF"
    DAS = "DAS", "DAS"
    DAE = "DAE", "DAE"
    DJE = "DJE", "DJE"


class FiscalReportState(models.TextChoices):
    """The SITFIS lifecycle (A7.3), alternatives included."""

    SOLICITADO = "solicitado", "Solicitado"
    PROTOCOLO_RECEBIDO = "protocolo_recebido", "Protocolo recebido"
    AGUARDANDO = "aguardando", "Aguardando"
    EMITINDO = "emitindo", "Emitindo"
    EMITIDO = "emitido", "Emitido"
    NEGADO = "negado", "Negado"
    FALHA = "falha", "Falha"
    INCERTO = "incerto", "Incerto"
    PROTOCOLO_EXPIRADO = "protocolo_expirado", "Protocolo expirado"
    LIMITE_DE_TENTATIVAS = "limite_de_tentativas", "Limite de tentativas"


class FederalConsultation(TenantScopedModel):
    """One consultation receipt: question, clock, environment and outcome."""

    client = models.ForeignKey(
        "clients.ClientCompany", on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=24, choices=FederalConsultationKind.choices)
    # The arrecadacao window a payments consultation covered. A situation report is
    # an as-of answer, so both stay NULL for it.
    range_start = models.DateField(null=True, blank=True)
    range_end = models.DateField(null=True, blank=True)
    consulted_at = models.DateTimeField()
    outcome = models.CharField(
        max_length=24, choices=FederalConsultationOutcome.choices
    )
    # The opaque provider call id this receipt answers (todo 9's journal); blank only
    # for a consultation refused before any call existed.
    provider_ref = models.CharField(max_length=64, blank=True, default="")
    environment = models.CharField(max_length=16, choices=FederalEnvironment.choices)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Client-paired referent, one receipt per provider call, honest ranges."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "id"],
                name="federalconsultation_scope_id_uniq",
            ),
            # A replayed call finalization returns the receipt it already wrote.
            models.UniqueConstraint(
                fields=["tenant", "client", "kind", "provider_ref"],
                condition=~Q(provider_ref=""),
                name="federalconsultation_call_uniq",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=FederalConsultationKind.values),
                name="federalconsultation_known_kind",
            ),
            models.CheckConstraint(
                condition=Q(outcome__in=FederalConsultationOutcome.values),
                name="federalconsultation_known_outcome",
            ),
            models.CheckConstraint(
                condition=Q(environment__in=FederalEnvironment.values),
                name="federalconsultation_known_environment",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        kind=FederalConsultationKind.PAGAMENTOS,
                        range_start__isnull=False,
                        range_end__gte=models.F("range_start"),
                    )
                    | Q(
                        kind=FederalConsultationKind.SITUACAO_FISCAL,
                        range_start__isnull=True,
                        range_end__isnull=True,
                    )
                ),
                name="federalconsultation_range_by_kind",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "kind", "consulted_at"],
                name="federalconsultation_scope_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the receipt by kind, outcome and environment."""
        return f"{self.kind} {self.outcome} ({self.environment})"


class FederalPayment(TenantScopedModel):
    """One arrecadacao document the Receita reported; append-only."""

    client = models.ForeignKey(
        "clients.ClientCompany", on_delete=models.PROTECT, related_name="+"
    )
    consultation = models.ForeignKey(
        FederalConsultation, on_delete=models.PROTECT, related_name="payments"
    )
    numero_documento = models.CharField(max_length=32)
    tipo = models.CharField(max_length=8, choices=FederalPaymentType.choices)
    periodo_apuracao = models.DateField()
    data_arrecadacao = models.DateField()
    data_vencimento = models.DateField(null=True, blank=True)
    receita_codigo = models.CharField(max_length=16)
    receita_descricao = models.CharField(max_length=255, blank=True, default="")
    valor_total = models.DecimalField(max_digits=14, decimal_places=2)
    valor_principal = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    valor_multa = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    valor_juros = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    # The obligation a deterministic match rule (todo 15) bound this payment to;
    # never inferred from the amount.
    matched_obligation = models.ForeignKey(
        "obligations.Obligation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """One row per client document number; amounts are never negative."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "id"],
                name="federalpayment_scope_id_uniq",
            ),
            models.UniqueConstraint(
                fields=["tenant", "client", "numero_documento"],
                name="federalpayment_document_uniq",
            ),
            models.CheckConstraint(
                condition=Q(tipo__in=FederalPaymentType.values),
                name="federalpayment_known_tipo",
            ),
            models.CheckConstraint(
                condition=(
                    Q(valor_total__gte=0)
                    & (Q(valor_principal__isnull=True) | Q(valor_principal__gte=0))
                    & (Q(valor_multa__isnull=True) | Q(valor_multa__gte=0))
                    & (Q(valor_juros__isnull=True) | Q(valor_juros__gte=0))
                ),
                name="federalpayment_nonnegative_amounts",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "matched_obligation"],
                name="federalpayment_match_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the document by type and number."""
        return f"{self.tipo} {self.numero_documento}"


class FiscalSituationReport(TenantScopedModel):
    """The SITFIS protocol and its emission; an as-of report, never month proof."""

    client = models.ForeignKey(
        "clients.ClientCompany", on_delete=models.PROTECT, related_name="+"
    )
    consultation = models.ForeignKey(
        FederalConsultation, on_delete=models.PROTECT, related_name="reports"
    )
    requested_at = models.DateTimeField()
    # Opaque provider protocol; not a credential, so not encrypted at rest.
    protocol = models.CharField(max_length=255, blank=True, default="")
    protocol_date = models.DateTimeField(null=True, blank=True)
    wait_until = models.DateTimeField(null=True, blank=True)
    emission_attempts = models.PositiveSmallIntegerField(default=0)
    emitted_at = models.DateTimeField(null=True, blank=True)
    # The emitted PDF. Firm-only: the migration's trigger refuses a portal-visible
    # document (INV-R10).
    document = models.ForeignKey(
        "obligations.Document",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    state = models.CharField(
        max_length=24,
        choices=FiscalReportState.choices,
        default=FiscalReportState.SOLICITADO,
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Known states and at most three emission attempts."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "id"],
                name="fiscalsituationreport_scope_id_uniq",
            ),
            models.CheckConstraint(
                condition=Q(state__in=FiscalReportState.values),
                name="fiscalsituationreport_known_state",
            ),
            models.CheckConstraint(
                condition=Q(emission_attempts__lte=MAX_EMISSION_ATTEMPTS),
                name="fiscalsituationreport_attempt_cap",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "state"],
                name="sitfisreport_scope_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the report by state."""
        return f"SITFIS {self.state}"

"""Firm SERPRO work, zero-cost termo reservations and PAGTOWEB consultations."""

from typing import TYPE_CHECKING, ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.cycles.records import ClientRecord

if TYPE_CHECKING:
    from apps.integrations.serpro.pagtoweb import PaymentRange

ACTIVE_STATES: Final = ("aberta", "em_tratamento")


class FirmIntegrationIssue(TenantScopedModel):
    """Owned firm work without inventing a taxpayer for a firm-level authorization."""

    connection = models.ForeignKey(
        "integrations.ProviderConnection", on_delete=models.PROTECT, related_name="+"
    )
    call = models.ForeignKey(
        "integrations.FirmProviderCall",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=40)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(
        max_length=16,
        default="aberta",
        choices=[
            ("aberta", "Aberta"),
            ("em_tratamento", "Em tratamento"),
            ("resolvida", "Resolvida"),
        ],
    )
    review_on = models.DateField()
    evidence_refs = ArrayField(
        models.CharField(max_length=64), default=list, blank=True
    )
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(TenantScopedModel.Meta):
        """Exactly one active condition per firm connection."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("tenant", "state"), name="firmissue_scope_idx")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "connection", "reason"),
                condition=Q(state__in=ACTIVE_STATES),
                name="firmissue_active_uniq",
            ),
        ]


class FirmProviderReservation(TenantScopedModel):
    """A real admission record for a free attempt, never a fabricated client balance."""

    call = models.ForeignKey(
        "integrations.FirmProviderCall", on_delete=models.PROTECT, related_name="+"
    )
    budget = models.ForeignKey(
        "integrations.ProviderBudget", on_delete=models.PROTECT, related_name="+"
    )
    attempt_number = models.PositiveIntegerField()
    microbrl = models.PositiveBigIntegerField(default=0)
    tariff_revision = models.CharField(max_length=64, default="serpro-free-v1")
    state = models.CharField(
        max_length=16,
        default="reserved",
        choices=[("reserved", "Reserved"), ("released", "Released")],
    )
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """One explicit zero reservation per client-less transmission."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("tenant", "call"), name="firmreservation_scope_idx")
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "call", "attempt_number"),
                name="firmreservation_attempt_uniq",
            ),
            models.CheckConstraint(
                condition=Q(microbrl=0), name="firmreservation_is_free"
            ),
            models.CheckConstraint(
                condition=Q(state__in=("reserved", "released")),
                name="firmreservation_known_state",
            ),
        ]


class PaymentQueryState(models.TextChoices):
    """One consultation's lifecycle; the end states mirror the receipt outcomes."""

    NA_FILA = "na_fila", "Na fila"
    EM_ANDAMENTO = "em_andamento", "Em andamento"
    ENCONTRADO = "encontrado", "Pagamentos encontrados"
    NADA_LOCALIZADO = "nada_localizado", "Nada localizado"
    SEM_PROCURACAO = "sem_procuracao", "Sem procuração"
    FALHA = "falha", "Falha"
    COTA_ESGOTADA = "cota_esgotada", "Limite de consultas atingido"
    INCERTO = "incerto", "Incerto"


ACTIVE_QUERY_STATES: Final = frozenset(
    {PaymentQueryState.NA_FILA, PaymentQueryState.EM_ANDAMENTO}
)
MAX_PAYMENT_PAGES: Final = 10


class PaymentQuery(ClientRecord):
    """One PAGTOWEB consultation the firm asked for (or the monthly schedule opened).

    The aggregate the workspace shows: who asked, which arrecadacao window, how many
    pages were fetched and what it ended up meaning. Each page remains its own
    protocol call and todo 14 `FederalConsultation` receipt; nothing here replaces
    their per-call provenance.
    """

    link = models.ForeignKey(
        "integrations.ClientProviderLink", on_delete=models.PROTECT, related_name="+"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    trigger = models.CharField(
        max_length=16,
        choices=[("equipe", "Equipe"), ("agendada", "Agendada")],
        default="agendada",
    )
    request_key = models.CharField(max_length=64)
    range_start = models.DateField()
    range_end = models.DateField()
    revenue_codes = ArrayField(models.CharField(max_length=4), default=list, blank=True)
    environment = models.CharField(max_length=16)
    state = models.CharField(
        max_length=24,
        choices=PaymentQueryState.choices,
        default=PaymentQueryState.NA_FILA,
    )
    pages_fetched = models.PositiveSmallIntegerField(default=0)
    payments_found = models.PositiveIntegerField(default=0)
    matched_count = models.PositiveIntegerField(default=0)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # The runner that is paying for this consultation's pages settles it alone
    # until this lease (database time) runs out; `integrations.0008` refuses any
    # other writer while it is live.
    settlement_owner = models.UUIDField(null=True, blank=True)
    settlement_until = models.DateTimeField(null=True, blank=True)

    class Meta(ClientRecord.Meta):
        """One intent per key, one active consultation per client, honest counts."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            *ClientRecord.Meta.constraints,
            models.UniqueConstraint(
                fields=("tenant", "client", "request_key"),
                name="paymentquery_request_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client"),
                condition=Q(state__in=("na_fila", "em_andamento")),
                name="paymentquery_one_active",
            ),
            models.CheckConstraint(
                condition=Q(state__in=PaymentQueryState.values),
                name="paymentquery_known_state",
            ),
            models.CheckConstraint(
                condition=Q(trigger="equipe", requested_by__isnull=False)
                | Q(trigger="agendada", requested_by__isnull=True),
                name="paymentquery_trigger_actor",
            ),
            models.CheckConstraint(
                condition=Q(range_end__gte=models.F("range_start")),
                name="paymentquery_range_order",
            ),
            models.CheckConstraint(
                condition=Q(pages_fetched__lte=MAX_PAYMENT_PAGES),
                name="paymentquery_page_cap",
            ),
            models.CheckConstraint(
                condition=Q(
                    settlement_owner__isnull=True, settlement_until__isnull=True
                )
                | Q(settlement_owner__isnull=False, settlement_until__isnull=False),
                name="paymentquery_owner_lease",
            ),
        ]

    def __str__(self) -> str:
        """Identify the consultation by window and state, never by taxpayer."""
        return f"PAGTOWEB {self.range_start}..{self.range_end} {self.state}"

    @property
    def window(self) -> "PaymentRange":
        """The validated question this consultation asks."""
        from apps.integrations.serpro.pagtoweb import PaymentRange  # noqa: PLC0415

        return PaymentRange(self.range_start, self.range_end, tuple(self.revenue_codes))

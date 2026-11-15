"""Client-scoped capture positions, fenced runs and immutable XML acquisition work."""

from typing import ClassVar, Final

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TenantScopedModel

COLLECTIONS: Final = [
    ("nfe_recebidas", "NF-e recebidas"),
    ("nfsen_recebidas", "NFS-e Nacional recebidas"),
]
RUN_STATES: Final = [
    (value, value)
    for value in (
        "em_fila",
        "executando",
        "concluida",
        "parcial",
        "falhou",
        "bloqueada",
    )
]
ACTIVE_RUN_STATES: Final = ("em_fila", "executando")


class SyncStream(TenantScopedModel):
    """One authoritative Focus version cursor within one immutable stream epoch."""

    client = models.ForeignKey("clients.ClientCompany", on_delete=models.PROTECT)
    link = models.ForeignKey(
        "integrations.ClientProviderLink", on_delete=models.PROTECT, related_name="+"
    )
    collection = models.CharField(max_length=24, choices=COLLECTIONS)
    filter_digest = models.CharField(max_length=64)
    epoch = models.PositiveIntegerField(default=1)
    committed_cursor = models.CharField(max_length=64, default="0")
    cursor_version = models.PositiveBigIntegerField(default=1)
    latest_attempt_at = models.DateTimeField(null=True, blank=True)
    last_api_success_at = models.DateTimeField(null=True, blank=True)
    last_successful_source_observation_at = models.DateTimeField(null=True, blank=True)
    last_complete_scan_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=24, default="aguardando")
    consecutive_failures = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """Private stream identity and tenant-led pending-work lookup."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "client", "id"), name="syncstream_scope_id_uniq"
            ),
            models.UniqueConstraint(
                fields=(
                    "tenant",
                    "client",
                    "link",
                    "collection",
                    "filter_digest",
                    "epoch",
                ),
                name="syncstream_identity_uniq",
            ),
            models.CheckConstraint(
                condition=Q(epoch__gte=1), name="syncstream_positive_epoch"
            ),
            models.CheckConstraint(
                condition=Q(collection__in=[value for value, _ in COLLECTIONS]),
                name="syncstream_known_collection",
            ),
            models.CheckConstraint(
                condition=Q(committed_cursor__regex=r"^[0-9]{1,64}$"),
                name="syncstream_numeric_cursor",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "link"], name="syncstream_scope_idx"
            )
        ]


class SyncRun(TenantScopedModel):
    """A bounded occurrence; a stale lease or fence cannot settle another page."""

    client = models.ForeignKey("clients.ClientCompany", on_delete=models.PROTECT)
    stream = models.ForeignKey(SyncStream, on_delete=models.PROTECT, related_name="+")
    occurrence_key = models.CharField(max_length=64)
    trigger = models.CharField(max_length=16, default="agendada")
    state = models.CharField(max_length=16, choices=RUN_STATES, default="em_fila")
    start_cursor = models.CharField(max_length=64, default="0")
    end_cursor = models.CharField(max_length=64, default="0")
    fence = models.PositiveBigIntegerField(default=1)
    lease_owner = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    disable_epoch = models.PositiveIntegerField(default=0)
    binding_snapshot = models.JSONField(default=dict)
    pages_committed = models.PositiveSmallIntegerField(default=0)
    requests_made = models.PositiveSmallIntegerField(default=0)
    documents_accounted = models.PositiveIntegerField(default=0)
    skipped_before_start = models.PositiveIntegerField(default=0)
    page_limit = models.PositiveSmallIntegerField(default=3)
    request_limit = models.PositiveSmallIntegerField(default=60)
    last_error_code = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """One active owner and one occurrence identity per client stream."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "client", "id"), name="syncrun_scope_id_uniq"
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "stream", "occurrence_key"),
                name="syncrun_occurrence_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "stream"),
                condition=Q(state__in=ACTIVE_RUN_STATES),
                name="syncrun_one_active_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "stream", "id"),
                name="syncrun_stream_id_uniq",
            ),
            models.CheckConstraint(
                condition=Q(state__in=[value for value, _ in RUN_STATES]),
                name="syncrun_known_state",
            ),
            models.CheckConstraint(
                condition=Q(page_limit__gte=1, page_limit__lte=3),
                name="syncrun_page_limit",
            ),
            models.CheckConstraint(
                condition=Q(request_limit__gte=1, request_limit__lte=60),
                name="syncrun_request_limit",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "stream"], name="syncrun_scope_idx"
            )
        ]


class CaptureAcquisition(TenantScopedModel):
    """The page's exact-revision XML work: retained bytes or explicitly pending."""

    client = models.ForeignKey("clients.ClientCompany", on_delete=models.PROTECT)
    stream = models.ForeignKey(SyncStream, on_delete=models.PROTECT, related_name="+")
    run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="+")
    revision = models.ForeignKey(
        "obligations.FiscalRevision", on_delete=models.PROTECT, related_name="+"
    )
    role = models.CharField(
        max_length=16,
        choices=[("xml_original", "xml_original"), ("xml_cce", "xml_cce")],
    )
    sequence = models.PositiveIntegerField(default=0)
    state = models.CharField(
        max_length=16, choices=[("pendente", "pendente"), ("retida", "retida")]
    )
    document = models.ForeignKey(
        "obligations.Document", on_delete=models.PROTECT, null=True, related_name="+"
    )
    call = models.ForeignKey(
        "integrations.ProviderCall",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        """Immutable work identity and an explicit retained-byte condition."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("tenant", "client", "id"),
                name="captureacquisition_scope_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("tenant", "client", "revision", "role", "sequence"),
                name="captureacquisition_revision_uniq",
            ),
            models.CheckConstraint(
                condition=Q(state="pendente", document__isnull=True)
                | Q(state="retida", document__isnull=False),
                name="captureacquisition_retained_bytes",
            ),
            models.CheckConstraint(
                condition=Q(role="xml_original", sequence=0)
                | Q(role="xml_cce", sequence__gt=0),
                name="captureacquisition_known_role",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "client", "stream"],
                name="captureacquisition_scope_idx",
            )
        ]

"""A portfolio import: one uploaded file, planned row by row, committed as one batch.

`ImportBatch` is the unit a firm commits and rolls back; `ImportRow` is the per-row
outcome of the dry run and, after commit, the provenance link to the client it
created. Both are firm-internal: the portal role holds no grant on either (plan S8.2),
and the RLS policies in the migration still confine a mistaken grant — the batch is
denied outright (it has no client), the row is anchored on its own `client_id`.

Rows are history. A rollback deletes the clients a batch created and leaves every row
in place with `client` nulled, so "what did this file try to do, and what happened"
stays answerable after the portfolio no longer shows it.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel


class ImportFormat(models.TextChoices):
    """The file formats the wizard reads."""

    CSV = "csv", _("CSV")
    XLSX = "xlsx", _("XLSX")


class ImportStatus(models.TextChoices):
    """Where a batch sits between its dry run and its end."""

    PLANNED = "planned", _("planned")
    QUEUED = "queued", _("queued")
    COMMITTED = "committed", _("committed")
    # Some planned rows failed to write at commit time; they can be retried.
    PARTIAL = "partial", _("partially committed")
    ROLLED_BACK = "rolled_back", _("rolled back")


class RowOutcome(models.TextChoices):
    """What the dry run decided for one row (S7.1).

    `UPDATED` is part of the contract but R1 never produces it: an import neither
    rewrites an existing client nor changes its CNPJ, so a CNPJ already in the firm
    is always `DUPLICATE_SKIPPED`.
    """

    CREATED = "created", _("created")
    UPDATED = "updated", _("updated")
    DUPLICATE_SKIPPED = "duplicate_skipped", _("duplicate skipped")
    INVALID = "invalid", _("invalid")


class ImportBatch(TenantScopedModel):
    """One uploaded file's dry run and, once committed, its provenance anchor."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="client_import_batches",
        verbose_name=_("created by"),
        null=True,
        blank=True,
    )
    filename = models.CharField(_("file name"), max_length=255)
    file_format = models.CharField(
        _("file format"),
        max_length=8,
        choices=ImportFormat.choices,
    )
    file_sha256 = models.CharField(_("file SHA-256"), max_length=64)
    # target field -> source column header, as confirmed by the person importing.
    mapping = models.JSONField(_("column mapping"), default=dict)
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=ImportStatus.choices,
        default=ImportStatus.PLANNED,
    )
    # Optimistic concurrency: commit and rollback carry the version they read.
    version = models.PositiveIntegerField(_("version"), default=1)
    total_rows = models.PositiveIntegerField(_("rows"), default=0)
    planned_created = models.PositiveIntegerField(_("rows to create"), default=0)
    planned_invalid = models.PositiveIntegerField(_("invalid rows"), default=0)
    planned_duplicate = models.PositiveIntegerField(_("duplicate rows"), default=0)
    committed_count = models.PositiveIntegerField(_("clients created"), default=0)
    failed_count = models.PositiveIntegerField(_("rows failed at commit"), default=0)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    committed_at = models.DateTimeField(_("committed at"), null=True, blank=True)
    rolled_back_at = models.DateTimeField(_("rolled back at"), null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("client import batch")
        verbose_name_plural = _("client import batches")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # The referent of the tenant-paired FKs from ImportRow.batch and
            # ClientCompany.import_batch.
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="importbatch_tenant_id_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "created_at"],
                name="importbatch_tenant_created_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the batch by file and status."""
        return f"{self.filename} ({self.status})"


class ImportRow(TenantScopedModel):
    """One data row of an import file and what became of it."""

    batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="rows",
        verbose_name=_("batch"),
    )
    # 1-based data row number as the person sees it in their spreadsheet: the
    # header is line 1, so the first data row is 2.
    row_number = models.PositiveIntegerField(_("row number"))
    outcome = models.CharField(
        _("outcome"),
        max_length=20,
        choices=RowOutcome.choices,
    )
    # The CNPJ as normalized from the file, kept even when invalid so the error
    # report can say what was read. Wider than 14 on purpose: invalid input is
    # truncated to this width, never refused for length here.
    cnpj = models.CharField(_("CNPJ"), max_length=32, blank=True)
    # The normalized values that would be written, keyed by ClientCompany field.
    values = models.JSONField(_("values"), default=dict)
    errors = models.JSONField(_("errors"), default=list)
    # Set when commit creates the client; nulled (not cascaded) when a rollback
    # deletes it, so the row survives as history.
    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.SET_NULL,
        related_name="import_rows",
        verbose_name=_("client"),
        null=True,
        blank=True,
    )
    commit_error = models.CharField(_("commit error"), max_length=255, blank=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("client import row")
        verbose_name_plural = _("client import rows")
        ordering: ClassVar[list[str]] = ["row_number"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Client-scoped uniqueness: a client is created by at most one row of a
            # batch. client_id is in the key so a collision is only reachable inside
            # the caller's own client (tests/isolation/test_client_scoped_uniqueness).
            models.UniqueConstraint(
                fields=["tenant", "client", "batch"],
                name="importrow_tenant_client_batch_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "batch", "row_number"],
                name="importrow_tenant_batch_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the row by batch and number."""
        return f"{self.batch_id}#{self.row_number} ({self.outcome})"

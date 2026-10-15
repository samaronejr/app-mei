"""Which recurring service the firm sells this client, and for which period.

An engagement is the durable fact a service cycle hangs off: "this firm runs the
monthly MEI pack for this client". It answers *what is owed per period*; the tax
profile answers *which rules apply*, and the two change on different clocks, which is
why they are different tables.

`provenance` names how the row came to exist — `'backfill_v1'` marks rows the data
migration derived from the legacy `is_mei` flag, so a portfolio review can tell a
human's answer from a machine's guess without inventing a status column for it.

`version` is the optimistic-concurrency counter every firm-side edit checks and
increments, so two staff members saving over each other cannot silently interleave.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel


class ServiceKind(models.TextChoices):
    """The recurring packs the registry knows how to name in R1."""

    MEI_MENSAL = "mei_mensal", _("monthly MEI pack")
    DOCUMENTOS_MENSAL = "documentos_mensal", _("monthly documents pack")
    OUTRO = "outro", _("other")


class Engagement(TenantScopedModel):
    """One service the firm performs for one client, from a start date."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="engagements",
        verbose_name=_("client"),
    )
    service_kind = models.CharField(
        _("service kind"),
        max_length=32,
        choices=ServiceKind.choices,
    )
    starts_on = models.DateField(_("starts on"))
    ends_on = models.DateField(_("ends on"), null=True, blank=True)
    # The accountable person inside the firm, when one has been named. A plain FK to
    # the global user table — like ClientAssignment.user, roster correctness is not
    # expressible as a database key, and unlike the assignment it confers no access,
    # so the defence is the tenant-paired composite FK on the client column.
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="responsible_engagements",
        verbose_name=_("responsible"),
        null=True,
        blank=True,
    )
    provenance = models.CharField(
        _("provenance"),
        max_length=32,
        help_text=_("how this row was created (e.g. backfill_v1, staff entry)"),
    )
    # Optimistic concurrency: every edit path carries the version it read, and the
    # write refuses to land on a row that moved. Incremented by services on update.
    version = models.PositiveIntegerField(_("version"), default=1)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("engagement")
        verbose_name_plural = _("engagements")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One open registration per service per start date, per client. client_id
            # is in the key because a collision on a constraint that omits it is a
            # one-bit existence oracle for another client's rows.
            models.UniqueConstraint(
                fields=["tenant", "client", "service_kind", "starts_on"],
                name="engagement_client_kind_starts_uniq",
            ),
            # The referent client-paired foreign keys in later waves point at.
            models.UniqueConstraint(
                fields=["tenant", "client", "id"],
                name="engagement_tenant_client_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_on__isnull=True)
                | models.Q(ends_on__gt=models.F("starts_on")),
                name="engagement_ends_after_starts",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads so the row-level-security predicate can be served by
            # the index rather than a sequential scan.
            models.Index(
                fields=["tenant", "client"], name="engagement_tenant_client_idx"
            ),
        ]

    def __str__(self) -> str:
        """Identify the engagement by service and client."""
        return f"{self.client_id} / {self.service_kind} from {self.starts_on}"

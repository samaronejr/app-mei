"""Owner-approved, time- and purpose-limited support access (AXR1-023, S8.3).

`SupportGrant` is the *only* way a platform account reaches a firm's data without
a seat in it. It is deliberately tenant-scoped like every other business table:
the row names one firm and is policed by the same `app.tenant_id` RLS policy. The
two places that must see it *before* a tenant context can be granted — the admin
tenant selector and the firm-host middleware — read it inside an explicit
`tenant_context` they open for exactly that read, so the policy still decides
the answer; nothing here bypasses row-level security.

The window is enforced twice, because each layer catches what the other cannot:
the `support_grant_72h_window` CHECK makes a forged row impossible, and
`resolve_support_grant`'s `is_active` predicate re-evaluates `expires_at` against
the wall clock on EVERY request — a grant that lapses mid-session refuses the
next request rather than finishing the one it started.
"""

from datetime import datetime, timedelta
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel

MAX_SUPPORT_WINDOW: timedelta = timedelta(hours=72)


class SupportScope(models.TextChoices):
    """How far an approved grant reaches into the firm."""

    READ_ONLY = "read_only", _("read only")
    READ_WRITE_SETTINGS = "read_write_settings", _("read and firm settings")


class SupportGrant(TenantScopedModel):
    """One request plus the owner's decision, never silently renewable.

    The lifecycle is two timestamps and a revocation:

    * `approved_by_owner` NULL — the request is pending and admits nothing.
    * approved and inside [`starts_at`, `expires_at`) — live.
    * `revoked_at` set or `expires_at` past — dead forever. Re-activating a dead
      row is refused by `approve_support_grant` and by the CHECK: a new session
      needs a new request and a new approval, which is the whole point of the
      "no silent extension" rule.
    """

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_requests",
        verbose_name=_("requested by"),
    )
    approved_by_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="support_approvals",
        verbose_name=_("approved by"),
    )
    purpose = models.CharField(_("purpose"), max_length=500)
    scope = models.CharField(
        _("scope"),
        max_length=32,
        choices=SupportScope.choices,
        default=SupportScope.READ_ONLY,
    )
    # Client primary keys as text, empty meaning firm-level metadata only — never
    # "all clients". Written by the approving owner, not by the requester.
    client_ids = models.JSONField(_("client ids"), default=list, blank=True)
    starts_at = models.DateTimeField(_("starts at"), null=True, blank=True)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("support grant")
        verbose_name_plural = _("support grants")
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads: the RLS predicate filters on it, and an index that
            # does not lead with it stops serving that predicate.
            models.Index(
                fields=["tenant", "requested_by", "-created_at"],
                name="support_grant_actor_idx",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(
                    expires_at__gt=models.F("starts_at"),
                    expires_at__lte=models.F("starts_at") + MAX_SUPPORT_WINDOW,
                )
                | models.Q(starts_at__isnull=True, expires_at__isnull=True),
                name="support_grant_72h_window",
            ),
            # A decision has exactly one shape: both approval fields set together.
            # Half-decided rows are storage bugs, not states.
            models.CheckConstraint(
                condition=(
                    models.Q(approved_by_owner__isnull=True, approved_at__isnull=True)
                    | models.Q(
                        approved_by_owner__isnull=False,
                        approved_at__isnull=False,
                    )
                ),
                name="support_grant_decision_complete",
            ),
            # One open request per (tenant, requester): a second pending row would
            # split the owner's decision surface without adding a fact.
            models.UniqueConstraint(
                fields=["tenant", "requested_by"],
                condition=models.Q(
                    approved_by_owner__isnull=True, revoked_at__isnull=True
                ),
                name="support_grant_one_pending",
            ),
        ]

    def is_active(self, *, at: datetime | None = None) -> bool:
        """Report whether this row admits a request right now."""
        moment = at or timezone.now()
        return bool(
            self.approved_by_owner_id is not None
            and self.revoked_at is None
            and self.starts_at is not None
            and self.expires_at is not None
            and self.starts_at <= moment < self.expires_at
        )

    def client_uuid_set(self) -> set[str]:
        """Return the approved client ids as strings, empty for firm-level only."""
        return {str(value) for value in (self.client_ids or [])}

    def __str__(self) -> str:
        """Identify the grant by who asked and for which firm."""
        return f"{self.requested_by_id} @ {self.tenant_id} ({self.scope})"


__all__ = [
    "MAX_SUPPORT_WINDOW",
    "SupportGrant",
    "SupportScope",
]

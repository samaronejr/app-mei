"""What a client still needs before the firm can actually work for them.

The checklist is two tables rather than a hard-coded list, and the split is the point.

`OnboardingItemTemplate` is **platform level**: the set of things a MEI must have in
order is a property of Brazilian tax practice, not of any one firm, and a per-tenant
copy would let one firm's list silently drift from another's. It is therefore listed in
`apps.core.rls.NON_TENANT_TABLES`, which the coverage meta-test's inverse check makes a
deliberate act rather than an omission.

`OnboardingItem` is the tenant's own copy, one row per client per template row, and
carries the status. Adding a template row is what adds an item to new clients — Phase 3
connectors get their own checklist entries without a migration, which is the whole
reason this is data.
"""

from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel, UUIDv7PrimaryKeyModel


class OnboardingStatus(models.TextChoices):
    """Where one checklist item stands."""

    PENDING = "pending", _("pending")
    BLOCKED = "blocked", _("blocked")
    DONE = "done", _("done")
    NOT_APPLICABLE = "not_applicable", _("not applicable")

    @classmethod
    def unfinished(cls) -> frozenset[str]:
        """Return the statuses that keep a client from being ready."""
        return frozenset({cls.PENDING, cls.BLOCKED})


class OnboardingItemTemplate(UUIDv7PrimaryKeyModel):
    """One row of the standard checklist, shared by every firm."""

    key = models.CharField(_("key"), max_length=64, unique=True)
    label = models.CharField(_("label"), max_length=255)
    position = models.PositiveSmallIntegerField(_("position"), default=0)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        """Model metadata."""

        verbose_name = _("onboarding item template")
        verbose_name_plural = _("onboarding item templates")
        ordering: ClassVar[list[str]] = ["position", "key"]

    def __str__(self) -> str:
        """Identify the template row by the key items are created from."""
        return self.key


class OnboardingItem(TenantScopedModel):
    """One checklist item for one client, with the firm's own progress on it."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="onboarding_items",
        verbose_name=_("client"),
    )
    # The key and label are copied from the template rather than referenced through a
    # foreign key. A template row may later be retired or reworded, and a client's
    # historical checklist must keep saying what was actually asked of them at the
    # time — an item that silently re-labels itself is an audit problem, not a feature.
    key = models.CharField(_("key"), max_length=64)
    label = models.CharField(_("label"), max_length=255)
    position = models.PositiveSmallIntegerField(_("position"), default=0)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=OnboardingStatus.choices,
        default=OnboardingStatus.PENDING,
    )
    blocked_reason = models.CharField(_("blocked reason"), max_length=255, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("onboarding item")
        verbose_name_plural = _("onboarding items")
        ordering: ClassVar[list[str]] = ["position", "key"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "client", "key"],
                name="onboardingitem_tenant_client_key_uniq",
            ),
            # A blocked item with no reason is the failure mode this table exists to
            # prevent: a client stuck with nobody able to say on what. Enforced in the
            # database so a management command or a future API cannot write one.
            models.CheckConstraint(
                condition=~models.Q(status=OnboardingStatus.BLOCKED)
                | ~models.Q(blocked_reason=""),
                name="onboardingitem_blocked_needs_a_reason",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["tenant", "status"],
                name="onboarding_tenant_status_idx",
            ),
        ]

    def __str__(self) -> str:
        """Identify the item by what it asks for and where it stands."""
        return f"{self.key} ({self.status})"

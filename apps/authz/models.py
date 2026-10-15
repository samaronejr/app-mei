"""The permission matrix as rows, so changing it is a data edit and not a deploy.

Both tables are **platform level**: the matrix is the product's own policy, identical
for every firm, and a tenant-scoped copy would let one firm's rows drift from another's
while looking correct. They are therefore listed in `apps.core.rls.NON_TENANT_TABLES`,
and the coverage meta-test's inverse check is what makes that listing deliberate rather
than forgotten.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel, UUIDv7PrimaryKeyModel


class Role(models.TextChoices):
    """Every role the source matrix scores, which is more than can be assigned today.

    `OWNER`, `STAFF_ACCOUNTANT` and `OPERATIONS_ADMIN` are exactly the values
    `tenants.TenantRole` offers, so a membership always resolves to a row here.

    The other three have no assignment path in this phase and that is intended, not
    an omission:

    * `PLATFORM_ADMIN` is never stored on a `Membership`. `can()` short-circuits on
      `User.is_superuser` before the membership lookup, which covers the report's
      platform-admin row without migrating the already-shipped membership model.
    * `CLIENT_OWNER` and `CLIENT_COLLABORATOR` are seeded now and reachable only from
      the Phase 2 client portal. Seeding them early keeps the matrix a faithful copy
      of the published one, so the portal arrives as a login path rather than as a
      second, hurried authorization design.
    """

    PLATFORM_ADMIN = "platform_admin", _("platform admin")
    OWNER = "owner", _("firm owner")
    STAFF_ACCOUNTANT = "staff_accountant", _("staff accountant")
    OPERATIONS_ADMIN = "operations_admin", _("firm operations admin")
    CLIENT_OWNER = "client_owner", _("MEI client owner")
    CLIENT_COLLABORATOR = "client_collaborator", _("MEI client collaborator")


class GrantLevel(models.TextChoices):
    """How much of a capability a role holds.

    Seven values, because the source matrix uses seven distinct markers. A four-value
    enum cannot express `View / confirm`, `Initiate / approve` or `Ticket only`, which
    would make "the seeded matrix matches the published one" unsatisfiable rather than
    merely inconvenient.

    * `FULL` (✅) — the capability, unrestricted within the tenant.
    * `NONE` (❌) — denied outright.
    * `LIMITED` — allowed only for objects the holder is attached to. Resolved by
      `can()` against the object: for a client, that means an assignment exists.
    * `VIEW_CONFIRM` — may read the workflow and confirm the outcome, never start it.
    * `INITIATE_APPROVE` — may ask for the action and approve it, never perform it;
      a firm-side role executes.
    * `OWN` — allowed only where the holder is the actor on the record.
    * `TICKET_ONLY` — reachable through the support queue rather than directly.

    `LIMITED` and `OWN` are the two that need the object, which is why `can()` takes
    one and is a single function rather than a decorator per level.
    """

    FULL = "full", _("full")
    NONE = "none", _("none")
    LIMITED = "limited", _("limited")
    VIEW_CONFIRM = "view_confirm", _("view / confirm")
    INITIATE_APPROVE = "initiate_approve", _("initiate / approve")
    OWN = "own", _("own actions only")
    TICKET_ONLY = "ticket_only", _("ticket only")


class Capability(UUIDv7PrimaryKeyModel):
    """One row of the published matrix, named by a slug that view code can ask for."""

    slug = models.CharField(_("slug"), max_length=64, unique=True)
    # The row's wording in deep-research-report.md, kept verbatim. It is the join key
    # the matrix test uses to compare the seeded rows against the published table, so
    # editing it to read better silently breaks that comparison.
    label = models.CharField(_("label"), max_length=128, unique=True)

    class Meta:
        """Model metadata."""

        verbose_name = _("capability")
        verbose_name_plural = _("capabilities")
        ordering: ClassVar[list[str]] = ["slug"]

    def __str__(self) -> str:
        """Identify the capability by the slug call sites use."""
        return self.slug


class RoleGrant(UUIDv7PrimaryKeyModel):
    """What one role holds of one capability."""

    role = models.CharField(_("role"), max_length=32, choices=Role.choices)
    capability = models.ForeignKey(
        Capability,
        on_delete=models.CASCADE,
        related_name="grants",
        verbose_name=_("capability"),
    )
    level = models.CharField(_("level"), max_length=24, choices=GrantLevel.choices)
    version = models.PositiveIntegerField(default=1, db_default=1, editable=False)

    class Meta:
        """Model metadata."""

        verbose_name = _("role grant")
        verbose_name_plural = _("role grants")
        ordering: ClassVar[list[str]] = ["role"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["role", "capability"],
                name="rolegrant_role_capability_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="rolegrant_positive_version",
            ),
        ]

    def __str__(self) -> str:
        """Identify the cell by its coordinates and value."""
        return f"{self.role}/{self.capability_id}={self.level}"


class UserCapabilityGrant(TenantScopedModel):
    """One firm raising ONE account above its role on ONE capability.

    Tenant-scoped — deliberately unlike `Capability` and `RoleGrant`, which are the
    platform's own policy and identical for every firm. A grant is a fact ABOUT a
    firm: which of its accountants may approve client-facing effects. Copying it
    platform-wide would let one firm's roster decisions leak into another's, so the
    row carries `tenant` and the standard fail-closed policy, and `app_portal` is
    denied the table outright.

    `level` is CHECK-constrained to `full`/`none`: the levels that need an object
    (`limited`, `own`) cannot be decided at grant-write time either, and the channel
    levels would mint tokens that never grant an action. `resolve_level` still ranks
    the full enum defensively; this constraint is what keeps the "max" it computes
    a real choice rather than a stored accident.

    `revoked_at` marks the row dead rather than deleting it — the same shape
    `Invite.revoked_at` takes, and for the same reason: the record that permission
    was held is evidence, not clutter. One ACTIVE grant per (tenant, user,
    capability) is enforced as a partial unique index; history after revocation is
    allowed to repeat.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="capability_grants",
        verbose_name=_("user"),
    )
    capability = models.ForeignKey(
        Capability,
        on_delete=models.PROTECT,
        related_name="user_grants",
        verbose_name=_("capability"),
    )
    level = models.CharField(_("level"), max_length=24, choices=GrantLevel.choices)
    # PROTECT: an audit answer that names the grantor must survive the grantor's
    # account. NULL rather than a sentinel user for grants written by the platform
    # itself (a migration, a support flow), which have no firm account to name.
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="capability_grants_issued",
        verbose_name=_("granted by"),
    )
    granted_at = models.DateTimeField(_("granted at"), auto_now_add=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    version = models.PositiveIntegerField(default=1, db_default=1, editable=False)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("user capability grant")
        verbose_name_plural = _("user capability grants")
        ordering: ClassVar[list[str]] = ["-granted_at"]
        indexes: ClassVar[list[models.Index]] = [
            # tenant leads: the RLS predicate filters on it, and the grant lookup
            # in `resolve_level` adds user/capability on top. The partial unique
            # constraint below covers the same columns only for live rows.
            models.Index(
                fields=["tenant", "user", "capability"],
                name="usercapgrant_tenant_user_cap",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One live grant per account per capability per firm. Partial on
            # revoked_at so the historical rows a revoke leaves behind do not
            # collide with the grant that replaces them.
            models.UniqueConstraint(
                fields=["tenant", "user", "capability"],
                condition=models.Q(revoked_at__isnull=True),
                name="usercapabilitygrant_active_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    level__in=(GrantLevel.FULL, GrantLevel.NONE),
                ),
                name="usercapabilitygrant_level_full_or_none",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="usercapabilitygrant_positive_version",
            ),
        ]

    def __str__(self) -> str:
        """Identify the grant by holder, capability, level and revocation mark."""
        # No quoted "active"/"revoked" words here: the status-literal guard scans
        # apps/ for enum member values spelled as strings, and both of those words
        # are member values elsewhere in the codebase.
        revoked = "+revoked" if self.revoked_at is not None else ""
        return f"{self.user_id}/{self.capability_id}={self.level}{revoked}"

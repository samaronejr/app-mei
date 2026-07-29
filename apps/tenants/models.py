"""The tenancy root: an accounting firm, who belongs to it, and who may join it.

None of these three tables is row-level-security scoped, and that is deliberate. The
tenant-resolving middleware reads `Membership` to *discover* which tenant a request
belongs to, and that read necessarily happens before `app.tenant_id` can be set. Under
the fail-closed policy an RLS-scoped `Membership` would return zero rows there, so no
user could ever sign in. All three therefore live in `NON_TENANT_TABLES` and are
protected at the application layer by filtering on `request.user`.
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.access import PlatformScopedManager, TenantRootManager
from apps.core.models import UUIDv7PrimaryKeyModel

INVITE_TOKEN_BYTES = 32
INVITE_VALIDITY = timedelta(days=7)


class TenantRole(models.TextChoices):
    """Every role a membership can carry, firm-side and client-side.

    The five values match `authz.Role` character for character. A divergence would make
    `role_of()` return a string no `RoleGrant` row matches, which fails closed but
    silently — the caller is simply denied everything with no indication why.
    """

    OWNER = "owner", _("owner")
    STAFF_ACCOUNTANT = "staff_accountant", _("staff accountant")
    OPERATIONS_ADMIN = "operations_admin", _("operations admin")
    CLIENT_OWNER = "client_owner", _("MEI client owner")
    CLIENT_COLLABORATOR = "client_collaborator", _("MEI client collaborator")


# A membership is firm-side or client-side, never both, and the distinction decides
# whether `client` must be NULL. Enforced as a database CHECK rather than in `save()`,
# because the middleware and the authorization path both read these rows directly.
FIRM_ROLES: frozenset[str] = frozenset(
    {
        TenantRole.OWNER,
        TenantRole.STAFF_ACCOUNTANT,
        TenantRole.OPERATIONS_ADMIN,
    },
)
CLIENT_ROLES: frozenset[str] = frozenset(
    {
        TenantRole.CLIENT_OWNER,
        TenantRole.CLIENT_COLLABORATOR,
    },
)


def firm_role_choices() -> list[tuple[str, str]]:
    """Return only the roles an invitation may grant.

    An invite creates a membership with no client, so offering a client role would
    produce a row the `membership_role_matches_client_scope` CHECK rejects — an
    IntegrityError at accept time rather than a validation error at issue time.
    Portal memberships are created against a specific client, never by invitation.
    """
    return [(role.value, str(role.label)) for role in TenantRole if role in FIRM_ROLES]


class TenantPlan(models.TextChoices):
    """Commercial tier. Entitlements are resolved as data, never as code branches."""

    TRIAL = "trial", _("trial")
    STANDARD = "standard", _("standard")
    ENTERPRISE = "enterprise", _("enterprise")


class Tenant(UUIDv7PrimaryKeyModel):
    """An accounting firm. The tenancy root, so it carries no tenant column itself."""

    name = models.CharField(_("name"), max_length=255)
    slug = models.SlugField(_("slug"), unique=True, max_length=63)
    plan = models.CharField(
        _("plan"),
        max_length=32,
        choices=TenantPlan.choices,
        default=TenantPlan.TRIAL,
    )
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = TenantRootManager["Tenant"]()

    class Meta:
        """Model metadata."""

        verbose_name = _("tenant")
        verbose_name_plural = _("tenants")
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        """Identify the firm by the subdomain it is reached at."""
        return self.slug


class Membership(UUIDv7PrimaryKeyModel):
    """A user's role within one firm. Read before any tenant context exists."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name=_("user"),
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name=_("tenant"),
    )
    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="portal_memberships",
        verbose_name=_("client"),
        null=True,
        blank=True,
    )
    role = models.CharField(_("role"), max_length=32, choices=TenantRole.choices)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    objects = PlatformScopedManager["Membership"]()

    class Meta:
        """Model metadata."""

        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # NULLS NOT DISTINCT is what preserves the Phase-1 guarantee. With the
            # PostgreSQL default, two firm-side rows both carrying client = NULL would
            # be considered distinct and a user could hold two roles in one firm.
            models.UniqueConstraint(
                fields=["user", "tenant", "client"],
                name="membership_user_tenant_client_uniq",
                nulls_distinct=False,
            ),
            # A firm role with a client, or a client role without one, is a membership
            # whose meaning is undefined: the middleware would grant firm-wide tenant
            # scope to a portal user, or leave a portal user with no client to resolve.
            models.CheckConstraint(
                condition=(
                    models.Q(role__in=FIRM_ROLES, client__isnull=True)
                    | models.Q(role__in=CLIENT_ROLES, client__isnull=False)
                ),
                name="membership_role_matches_client_scope",
            ),
        ]

    def __str__(self) -> str:
        """Identify the grant by who holds it, where, and as what."""
        return f"{self.user_id} @ {self.tenant_id} ({self.role})"


class Invite(UUIDv7PrimaryKeyModel):
    """A pending invitation to join a firm.

    `token` holds a SHA-256 digest, never the value handed to the invitee. An invite
    row is a bearer credential, so a database dump, a log line or an admin screenshot
    must not be enough to accept it.
    """

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="invites",
        verbose_name=_("tenant"),
    )
    email = models.EmailField(_("email address"))
    role = models.CharField(_("role"), max_length=32, choices=TenantRole.choices)
    token = models.CharField(_("token digest"), max_length=64, unique=True)
    expires_at = models.DateTimeField(_("expires at"))
    accepted_at = models.DateTimeField(_("accepted at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    objects = PlatformScopedManager["Invite"]()

    class Meta:
        """Model metadata."""

        verbose_name = _("invite")
        verbose_name_plural = _("invites")
        ordering: ClassVar[list[str]] = ["-created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Accepting an invite creates a membership with no client, so a client
            # role here would violate membership_role_matches_client_scope at accept
            # time — an IntegrityError for the invitee rather than a validation error
            # for the firm. The form offers firm roles only; this is the layer that
            # holds when something writes an Invite without going through the form.
            models.CheckConstraint(
                condition=models.Q(role__in=FIRM_ROLES),
                name="invite_role_is_firm_side",
            ),
        ]

    def __str__(self) -> str:
        """Identify the invitation by recipient and firm, never by token."""
        return f"{self.email} -> {self.tenant_id}"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Return the digest stored for a token presented by an invitee."""
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    def issue(
        cls,
        *,
        tenant: Tenant,
        email: str,
        role: str,
        expires_at: datetime | None = None,
    ) -> tuple["Invite", str]:
        """Create an invite and return it with the one-time raw token.

        The raw token is returned rather than stored so the caller can mail it. It is
        unrecoverable afterwards, which is the point.
        """
        raw_token = secrets.token_urlsafe(INVITE_TOKEN_BYTES)
        invite = cls.objects.create(
            tenant=tenant,
            email=email,
            role=role,
            token=cls.hash_token(raw_token),
            expires_at=expires_at or timezone.now() + INVITE_VALIDITY,
        )
        return invite, raw_token

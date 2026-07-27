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

from apps.core.models import UUIDv7PrimaryKeyModel

INVITE_TOKEN_BYTES = 32
INVITE_VALIDITY = timedelta(days=7)


class TenantRole(models.TextChoices):
    """Firm-side roles. Client-side roles arrive with the client registry in T-025."""

    OWNER = "owner", _("owner")
    STAFF_ACCOUNTANT = "staff_accountant", _("staff accountant")
    OPERATIONS_ADMIN = "operations_admin", _("operations admin")


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
    role = models.CharField(_("role"), max_length=32, choices=TenantRole.choices)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["user", "tenant"],
                name="membership_user_tenant_uniq",
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

    class Meta:
        """Model metadata."""

        verbose_name = _("invite")
        verbose_name_plural = _("invites")
        ordering: ClassVar[list[str]] = ["-created_at"]

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

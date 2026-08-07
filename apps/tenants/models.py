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
from typing import TYPE_CHECKING, ClassVar, Final

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.access import PlatformScopedManager, TenantRootManager
from apps.core.models import UUIDv7PrimaryKeyModel
from apps.tenants.validators import validate_tenant_slug

if TYPE_CHECKING:
    from apps.clients.models import ClientCompany

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


# The two roles a PORTAL invitation may grant, stated as a pair rather than filtered out
# of `TenantRole` the way `firm_role_choices` above filters its own.
#
# Two reasons, and the second is the load-bearing one. The order here is the order a
# chooser offers, and it is chosen — `client_owner` first, because that is the seat a
# MEI owner takes and the collaborator is the exception. Filtering `TenantRole` would
# inherit declaration order by accident instead.
#
# And `tests/authz/test_role_check_guard.py` pins this module to FIVE role references,
# character for character, as the price of its data-shape exemption. `if role in
# CLIENT_ROLES` would be a sixth, and the guard is explicit that a sixth has to be
# argued for rather than inherited. Nothing here needs to compare a role: the pair IS
# the answer. `CLIENT_ROLES` remains the authority the CHECK constraints below evaluate
# against, and `test_the_portal_choices_agree_with_the_check_constraint` holds the two
# together so this tuple cannot drift out of it.
PORTAL_INVITE_ROLES: Final[tuple["TenantRole", ...]] = (
    TenantRole.CLIENT_OWNER,
    TenantRole.CLIENT_COLLABORATOR,
)


def client_role_choices() -> list[tuple[str, str]]:
    """Return only the roles a PORTAL invitation may grant.

    The twin of `firm_role_choices`, and it exists for the mirror-image reason. A portal
    invitation creates a membership against one client, so offering a FIRM role would
    produce a row `membership_role_matches_client_scope` rejects — an IntegrityError at
    accept time, for the invitee, rather than a validation error at issue time, for the
    firm, which is the one moment anybody can still fix it.
    """
    return [(member.value, str(member.label)) for member in PORTAL_INVITE_ROLES]


class TenantPlan(models.TextChoices):
    """Commercial tier. Entitlements are resolved as data, never as code branches."""

    TRIAL = "trial", _("trial")
    STANDARD = "standard", _("standard")
    ENTERPRISE = "enterprise", _("enterprise")


class Tenant(UUIDv7PrimaryKeyModel):
    """An accounting firm. The tenancy root, so it carries no tenant column itself."""

    name = models.CharField(_("name"), max_length=255)
    slug = models.SlugField(
        _("slug"),
        unique=True,
        max_length=63,
        validators=[validate_tenant_slug],
    )
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
    # An invitation is a promise of a membership, so it carries the same optional
    # client the membership will. NULL means firm-side: the seat is in the firm. A
    # value means portal-side: the seat is inside that one client. Which of the two an
    # invitation is gets decided when it is issued, never when it is redeemed — the
    # token is already in the invitee's hands by then, and a seat that could be
    # re-scoped at accept time would be a bearer credential for an unbounded grant.
    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="portal_invites",
        verbose_name=_("client"),
        null=True,
        blank=True,
    )
    email = models.EmailField(_("email address"))
    role = models.CharField(_("role"), max_length=32, choices=TenantRole.choices)
    token = models.CharField(_("token digest"), max_length=64, unique=True)
    expires_at = models.DateTimeField(_("expires at"))
    accepted_at = models.DateTimeField(_("accepted at"), null=True, blank=True)
    # Withdrawal is recorded as a THIRD timestamp rather than by deleting the row or by
    # backdating `expires_at`, and both alternatives lose something this column keeps.
    #
    # A DELETE removes the only record that this address was ever offered a seat, and
    # it takes the token digest with it — so a link presented afterwards matches
    # nothing and is refused as an UNKNOWN invitation rather than a withdrawn one. The
    # holder is told the wrong thing, and the firm can no longer answer "did we invite
    # them, and did we take it back?".
    #
    # Backdating `expires_at` keeps the row but destroys the distinction the trail
    # exists for: "the firm changed its mind" and "nobody got round to it" become the
    # same fact, in the one column that could have told them apart.
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    objects = PlatformScopedManager["Invite"]()

    class Meta:
        """Model metadata."""

        verbose_name = _("invite")
        verbose_name_plural = _("invites")
        ordering: ClassVar[list[str]] = ["-created_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Accepting an invite writes Membership(client=invite.client), so this is
            # membership_role_matches_client_scope evaluated one step earlier, against
            # the same two arms. An invitation the membership constraint would reject
            # is an IntegrityError for the INVITEE at accept time — they arrive with a
            # valid token and cannot get in — rather than a refusal for the firm at
            # issue time, which is the one moment anyone can still fix it.
            #
            # The name predates the portal invitation and is kept deliberately:
            # renaming a CHECK is a drop-and-recreate that changes no behaviour, and
            # the arm it names is still the one that carries the most weight — a
            # client role on a client-less invite is how a portal seat escapes a
            # client and becomes firm-wide.
            models.CheckConstraint(
                condition=(
                    models.Q(role__in=FIRM_ROLES, client__isnull=True)
                    | models.Q(role__in=CLIENT_ROLES, client__isnull=False)
                ),
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
        client: "ClientCompany | None" = None,
        expires_at: datetime | None = None,
    ) -> tuple["Invite", str]:
        """Create an invite and return it with the one-time raw token.

        The raw token is returned rather than stored so the caller can mail it. It is
        unrecoverable afterwards, which is the point.

        `client` defaults to None, which is what makes every existing firm-side caller
        keep issuing firm-side invitations unchanged. A value makes this a portal
        invitation, and the pairing with `role` is checked by `invite_role_is_firm_side`
        in the database rather than here — one arm of that CHECK is the whole reason the
        column is on this row at all.
        """
        raw_token = secrets.token_urlsafe(INVITE_TOKEN_BYTES)
        invite = cls.objects.create(
            tenant=tenant,
            email=email,
            role=role,
            client=client,
            token=cls.hash_token(raw_token),
            expires_at=expires_at or timezone.now() + INVITE_VALIDITY,
        )
        return invite, raw_token

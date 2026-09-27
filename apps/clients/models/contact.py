"""The people at the client the firm may actually talk to.

A contact is a person, not an account: `membership` links to the portal seat only
when the contact has accepted an invitation, and most contacts in R1 never will —
the firm records who it emails and calls whether or not that person signs in.

`channel_email_allowed` exists because "we know this address" and "we may use it" are
different facts. A client who asks not to be emailed stays recorded — deleting them
would erase the refusal along with the contact.

The portal sees NONE of this table: `app_portal` holds no grant on it. The
`EnablePortalClientRLS` policy is still applied (the portal coverage meta-test
requires a client predicate on every client-scoped table), so a grant added by
mistake later still confines the role to its own client instead of opening the whole
firm's address book.
"""

from collections.abc import Iterable
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.base import ModelBase
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel
from apps.tenants.models import Membership


class ClientContact(TenantScopedModel):
    """One named person at a client company."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="contacts",
        verbose_name=_("client"),
    )
    name = models.CharField(_("name"), max_length=255)
    email = models.EmailField(_("email address"), blank=True)
    phone = models.CharField(_("phone"), max_length=32, blank=True)
    # What this person does for the client, in the firm's words — "contador
    # interno", "socio", "recepcao". Free text because a controlled vocabulary would
    # invent distinctions the firms do not share.
    role_label = models.CharField(_("role"), max_length=64, blank=True)
    # The portal seat this contact holds, when they have one. tenants_membership
    # is a tenancy root and carries no uniqueness a client-paired composite FK
    # could reference, so — as with ClientAssignment.user — the scope check is
    # enforced in save() below instead of by the database key itself.
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        related_name="contacts",
        verbose_name=_("portal membership"),
        null=True,
        blank=True,
    )
    channel_email_allowed = models.BooleanField(
        _("may email"),
        default=True,
    )
    is_primary = models.BooleanField(_("primary contact"), default=False)
    active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("client contact")
        verbose_name_plural = _("client contacts")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # The referent future client-paired foreign keys point at (T7 invites a
            # contact into the portal, and a later wave references that link).
            models.UniqueConstraint(
                fields=["tenant", "client", "id"],
                name="clientcontact_tenant_client_id_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # tenant_id leads so the row-level-security predicate can be served by
            # the index rather than a sequential scan.
            models.Index(fields=["tenant", "client"], name="contact_tenant_client_idx"),
        ]

    def __str__(self) -> str:
        """Identify the contact by name and client."""
        return f"{self.name} ({self.client_id})"

    def assert_membership_matches_client(self) -> None:
        """Refuse a seat link that would cross the client boundary.

        tenants_membership is deliberately unpoliced (the middleware reads it to
        discover the tenant before any context exists), so no database constraint
        can express this: the check lives here, the way ClientAssignment's roster
        check does, and a portal seat's client_id is what confines the link — a
        firm-side seat carries NULL and never matches.
        """
        if self.membership_id is None:
            return
        # PLATFORM_QUERY_OK: tenants_membership is a tenancy root, deliberately
        # unpoliced; the lookup IS the boundary check, not a query that needs one.
        # A dangling id fails the same way rather than leaking a DoesNotExist.
        try:
            membership = self.membership
        except Membership.DoesNotExist:
            membership = None
        belongs = (
            membership is not None
            and membership.tenant_id == self.tenant_id
            and membership.client_id == self.client_id
        )
        if not belongs:
            msg = (
                "A contact's portal seat must belong to the same client the "
                "contact does."
            )
            raise ValidationError({"membership": msg})

    def clean(self) -> None:
        """Surface the seat-scope check as a form error."""
        super().clean()
        self.assert_membership_matches_client()

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Enforce the seat-scope check on every write, not only on forms.

        Deliberately here rather than only in `clean`: `clean` runs for ModelForms
        and nothing else, so a management command or a future import would write
        straight past it — same shape as the roster guard on ClientAssignment.
        """
        self.assert_membership_matches_client()
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

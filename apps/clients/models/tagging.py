"""Free-form labels a firm puts on its own clients.

The join table is declared explicitly and inherits `TenantScopedModel`, which is the
whole point of this module rather than a stylistic preference. Django's implicit
many-to-many join table is generated with **no tenant column**, so it can carry no
row-level-security policy, and the coverage meta-test would have to allow-list it.
That table holds `(client_id, tag_id)` pairs and is writable through the ORM, which
makes it exactly the cross-tenant write vector the rest of the design closes — a
firm could attach its own tag to another firm's client, and the pair would be
invisible to the victim while remaining perfectly real.

Both foreign keys therefore also carry the composite `(tenant_id, …)` form, because
referential-integrity checks bypass row security in PostgreSQL.
"""

from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantScopedModel


class Tag(TenantScopedModel):
    """A label in one firm's own vocabulary."""

    name = models.CharField(_("name"), max_length=64)
    clients = models.ManyToManyField(
        "clients.ClientCompany",
        through="clients.ClientTag",
        related_name="tags",
        verbose_name=_("clients"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("tag")
        verbose_name_plural = _("tags")
        ordering: ClassVar[list[str]] = ["name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                name="tag_tenant_name_uniq",
            ),
            # The referent for ClientTag's composite foreign key, exactly as
            # ClientCompany carries one for the same reason.
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="tag_tenant_id_uniq",
            ),
        ]

    def __str__(self) -> str:
        """Identify the label by the word the firm chose."""
        return self.name


class ClientTag(TenantScopedModel):
    """One label on one client, as a table the isolation policy can reach."""

    client = models.ForeignKey(
        "clients.ClientCompany",
        on_delete=models.CASCADE,
        related_name="tag_links",
        verbose_name=_("client"),
    )
    tag = models.ForeignKey(
        Tag,
        on_delete=models.CASCADE,
        related_name="client_links",
        verbose_name=_("tag"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        verbose_name = _("client tag")
        verbose_name_plural = _("client tags")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # `tenant` leads for the same reason as on ClientAssignment: every index
            # on a policed table must lead with tenant_id to serve the RLS predicate.
            models.UniqueConstraint(
                fields=["tenant", "client", "tag"],
                name="clienttag_tenant_client_tag_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "tag"], name="clienttag_tenant_tag_idx"),
        ]

    def __str__(self) -> str:
        """Identify the link by what is labelled and with what."""
        return f"{self.client_id} #{self.tag_id}"

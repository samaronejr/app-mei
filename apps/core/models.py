"""Abstract bases every business table inherits.

`tenant_id` is denormalized onto every tenant-scoped table even where it is derivable
through a foreign key, because a row-level-security policy can only reference columns
on the row it is filtering. A policy cannot join.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.identifiers import uuid7
from apps.core.managers import TenantScopedManager


class UUIDv7PrimaryKeyModel(models.Model):
    """A primary key that is unguessable but still time-ordered.

    Sequential integers leak volume and allow enumeration across a tenant boundary;
    UUIDv4 fixes that but scatters every insert across the B-tree. UUIDv7 keeps the
    opacity and restores insert locality.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    class Meta:
        """Model metadata."""

        abstract = True


class TenantScopedModel(UUIDv7PrimaryKeyModel):
    """A row that belongs to exactly one accounting firm.

    Concrete subclasses must declare a composite index leading with `tenant`, and must
    be given an `EnableRLS` operation in the migration that creates them. The coverage
    meta-test in T-014 fails the build if either is missing.
    """

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        db_index=True,
        verbose_name=_("tenant"),
    )

    # all_objects is declared FIRST on purpose, and Meta.default_manager_name is what
    # stops that from mattering. Django resolves _default_manager to the first manager
    # by (mro depth, creation counter) unless the name overrides it, so without the
    # Meta entry below the UNSCOPED manager would silently become the default and
    # everything routed through _default_manager — admin get_queryset, dumpdata,
    # reverse-FK related managers, validate_unique — would run across every tenant.
    all_objects = models.Manager()
    objects = TenantScopedManager["TenantScopedModel"]()

    class Meta:
        """Model metadata.

        Concrete subclasses MUST inherit this class (`class Meta(TenantScopedModel.
        Meta):`). A subclass declaring a bare `class Meta:` replaces this one rather
        than merging with it, and both manager names become `None` on that subclass.

        That is latent rather than immediately fatal, which is what makes it
        dangerous: `Options.default_manager` falls back to the parent's name only
        `if not default_manager_name and not self.local_managers`. A subclass with no
        manager of its own therefore still resolves correctly, and the mistake stays
        invisible until someone adds a perfectly ordinary custom manager — at which
        point the fallback stops applying and the default silently becomes unscoped.
        The meta-test asserts the names on every subclass so it is caught at the
        commit that introduces it, not at the commit that weaponizes it.
        """

        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"

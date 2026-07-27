"""Abstract bases every business table inherits.

`tenant_id` is denormalized onto every tenant-scoped table even where it is derivable
through a foreign key, because a row-level-security policy can only reference columns
on the row it is filtering. A policy cannot join.
"""

import uuid6
from django.db import models
from django.utils.translation import gettext_lazy as _


class UUIDv7PrimaryKeyModel(models.Model):
    """A primary key that is unguessable but still time-ordered.

    Sequential integers leak volume and allow enumeration across a tenant boundary;
    UUIDv4 fixes that but scatters every insert across the B-tree. UUIDv7 keeps the
    opacity and restores insert locality.
    """

    id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)

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

    # The scoped manager arrives in T-007b, together with the Meta names that make it
    # the default. Until then this is the only manager, so nothing is silently scoped.
    all_objects = models.Manager()

    class Meta:
        """Model metadata."""

        abstract = True

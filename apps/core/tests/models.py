"""A concrete tenant-scoped model that exists purely to be asserted against."""

from typing import ClassVar

from django.db import models

from apps.core.models import TenantScopedModel


class ExampleTenantModel(TenantScopedModel):
    """The stand-in for every future business table.

    It carries the two properties the isolation machinery depends on: a `tenant`
    foreign key, and a composite index that leads with `tenant_id`.
    """

    name = models.CharField(max_length=100)

    # Inherits the parent Meta rather than declaring a bare one: a bare `class Meta:`
    # REPLACES the parent's and silently drops both manager names.
    class Meta(TenantScopedModel.Meta):
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "name"], name="coretests_tenant_name_idx"),
        ]

    def __str__(self) -> str:
        """Identify the row by its name."""
        return self.name

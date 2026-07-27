"""A concrete tenant-scoped model that exists purely to be asserted against."""

from typing import ClassVar

from django.db import models

from apps.core.managers import TenantScopedManager
from apps.core.models import TenantScopedModel


class ExampleTenantModel(TenantScopedModel):
    """The stand-in for every future business table.

    It carries the two properties the isolation machinery depends on: a `tenant`
    foreign key, and a composite index that leads with `tenant_id`.
    """

    name = models.CharField(max_length=100)

    # Named `scoped`, not `objects`: the Meta names that make a scoped manager the
    # default arrive in T-007b, and asserting through `objects` here would only prove
    # Django's manager-resolution order, not this manager's filtering.
    scoped = TenantScopedManager["ExampleTenantModel"]()

    class Meta:
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["tenant", "name"], name="coretests_tenant_name_idx"),
        ]

    def __str__(self) -> str:
        """Identify the row by its name."""
        return self.name

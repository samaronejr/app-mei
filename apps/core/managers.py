"""Managers that keep ordinary application code inside one tenant by default."""

from typing import TypeVar

from django.db import models

from apps.core.tenancy import current_tenant_id

_ModelT = TypeVar("_ModelT", bound=models.Model)


class TenantScopedManager(models.Manager[_ModelT]):
    """Filter every queryset by the tenant in context, and return nothing without one.

    Returning `none()` rather than everything is the point: it matches what the
    row-level-security policy already does, so a code path that loses tenant context
    behaves identically at both layers instead of one contradicting the other.
    """

    def get_queryset(self) -> models.QuerySet[_ModelT]:
        """Return only the rows belonging to the tenant in context."""
        tenant_id = current_tenant_id.get()
        if tenant_id is None:
            return super().get_queryset().none()
        return super().get_queryset().filter(tenant_id=tenant_id)

    def all_tenants(self) -> models.QuerySet[_ModelT]:
        """Return rows across every tenant, deliberately unscoped.

        Every call site must carry an adjacent `# ALL_TENANTS_OK: <reason>` comment.
        A guard test greps for calls without one and fails the build, which keeps this
        escape hatch rare, intentional, and reviewable with a single grep.

        This still only reaches rows the *database* allows. Under an unset
        `app.tenant_id` the row-level-security policy returns nothing regardless, so
        genuinely cross-tenant work must also enter `tenant_context` per tenant.
        """
        return super().get_queryset()

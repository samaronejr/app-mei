"""The positive control for tables that have NO database-layer isolation.

`NON_TENANT_TABLES` is assertion suppression, not protection. Listing a table there
tells the coverage meta-test to stop asking why it has no policy — it does not give it
one. `Tenant`, `Membership`, `Invite`, `AccessLog`, `PlatformEvent` and
`DataSubjectRequest` are all in that list for good reasons (the middleware must read
memberships before any tenant context can exist; a login failure has no tenant), and
the consequence is that for these six tables **the application layer is the only
layer**.

A convention to "filter on `request.user`" is not a control, because nothing fails
when someone forgets. So the filter is given a name — `for_user()` — which makes two
things possible that a convention cannot: a functional test per model proving the
scoping actually holds, and a grep proving every application query goes through it.
"""

from typing import TypeVar

from django.apps import apps as django_apps
from django.contrib.auth.models import AnonymousUser
from django.db import models

from apps.accounts.models import User

_ModelT = TypeVar("_ModelT", bound=models.Model)


class PlatformScopedManager(models.Manager[_ModelT]):
    """Scope a policy-free table to the firms the caller actually belongs to.

    `tenant_lookup` is the path from this model to a tenant id. It is `"tenant_id"`
    for everything that references a firm, and `"id"` on `Tenant` itself.
    """

    tenant_lookup: str = "tenant_id"

    def for_user(self, user: User | AnonymousUser) -> models.QuerySet[_ModelT]:
        """Return only the rows belonging to firms this account is a member of.

        Fail-closed for anonymous callers, matching what row-level security would do
        if these tables could carry a policy at all. Superusers are **not** special
        here: cross-tenant reach is a separate, audited, break-glass path, never a
        silent widening of an ordinary query.
        """
        if not user.is_authenticated:
            return self.get_queryset().none()
        membership = django_apps.get_model("tenants", "Membership")
        # Firm-side memberships only. A client-role row would inject the firm's
        # tenant id here, widening Tenant/Invite/AccessLog.for_user for a portal user.
        tenant_ids = membership.objects.filter(
            user=user,
            is_active=True,
            client__isnull=True,
        ).values("tenant_id")
        return self.get_queryset().filter(**{f"{self.tenant_lookup}__in": tenant_ids})


class TenantRootManager(PlatformScopedManager[_ModelT]):
    """`for_user` on the tenancy root itself, where the tenant id IS the primary key."""

    tenant_lookup = "id"

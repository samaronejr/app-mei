"""Which firm a platform operator is allowed to look at, and how that is recorded.

**This is the highest-severity control in the product.** The admin console is served
on the platform hostname, where `TenantMiddleware` resolves no tenant, so a tenant has
to be chosen explicitly. Choosing it is a cross-tenant read primitive unless it is
authorized, and it is invisible to both isolation layers *by design*: once the GUC and
the ContextVar say tenant B, row-level security correctly returns tenant B's rows and
the scoped manager correctly agrees. Nothing is violated. One `is_staff` account would
read every firm's CNPJs, CPFs, revenue and audit stream.

Three things follow, and all three are load-bearing:

1. **The submitted value is validated server-side.** `Tenant` is deliberately outside
   row-level security, so the option list is every firm on the platform. Filtering the
   dropdown changes nothing — the value arrives as a request parameter. Textbook IDOR.
2. **Superuser reach is break-glass, not a shortcut.** It requires a stated reason and
   emits an `impersonation` event naming actor, target and reason.
3. **Every selection is audited**, not only the break-glass ones. A platform operator
   reading a firm's client registry is processing personal data under LGPD, and a URL
   in the access log is not an adequate record of *which firm* was opened.
"""

from typing import Final
from uuid import UUID

from django.contrib.auth.models import AnonymousUser
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.tenants.models import Tenant

ADMIN_TENANT_SESSION_KEY: Final = "admin_tenant_id"


class AdminTenantError(Exception):
    """Base class for a refused tenant selection."""


class TenantNotSelectableError(AdminTenantError):
    """Raised when an account may not operate as the requested firm."""


class BreakGlassReasonRequiredError(AdminTenantError):
    """Raised when cross-tenant access is attempted with no reason stated."""


def selectable_tenants(user: User | AnonymousUser) -> QuerySet[Tenant]:
    """Return the firms this operator may select through the ordinary path."""
    return Tenant.objects.for_user(user)


def is_break_glass(user: User, tenant: Tenant) -> bool:
    """Report whether reaching this firm goes beyond the operator's own memberships."""
    return not selectable_tenants(user).filter(pk=tenant.pk).exists()


def authorize(*, user: User, tenant_id: UUID, reason: str = "") -> tuple[Tenant, bool]:
    """Authorize a selection server-side, returning the firm and whether it was forced.

    The submitted id is checked against the operator's own memberships. Filtering the
    dropdown is a UI convenience; THIS is the control, because the value arrives as a
    request parameter and a caller can send any id they like.
    """
    # PLATFORM_QUERY_OK: resolves the submitted id before authorizing it. The
    # authorization is the very next statement and is what actually gates access.
    tenant = Tenant.objects.filter(pk=tenant_id, is_active=True).first()
    if tenant is None:
        msg = "No such firm, or it is not active."
        raise TenantNotSelectableError(msg)

    if not is_break_glass(user, tenant):
        return tenant, False

    if not user.is_superuser:
        msg = "This account has no active membership for that firm."
        raise TenantNotSelectableError(msg)
    if not reason.strip():
        msg = "Cross-tenant access requires a stated reason."
        raise BreakGlassReasonRequiredError(msg)
    return tenant, True


def may_operate_as(user: User, tenant_id: UUID) -> bool:
    """Re-check a selection already held in the session.

    Called on every admin request rather than only at selection time, so that revoking
    a membership takes effect immediately instead of at the operator's next sign-in.
    """
    if user.is_superuser:
        return True
    return selectable_tenants(user).filter(pk=tenant_id).exists()


def record_selection(
    *,
    user: User,
    tenant: Tenant,
    break_glass: bool,
    reason: str = "",
) -> None:
    """Write the audit trail for a selection, including the break-glass case."""
    record_event(
        action=AuditAction.TENANT_SELECTED,
        tenant_id=tenant.pk,
        actor=user,
        obj=ObjectRef(type="Tenant", id=str(tenant.pk)),
        metadata={"break_glass": break_glass},
    )
    if not break_glass:
        return
    record_event(
        action=AuditAction.IMPERSONATION,
        tenant_id=tenant.pk,
        actor=user,
        obj=ObjectRef(type="Tenant", id=str(tenant.pk)),
        metadata={"reason": reason.strip()},
    )

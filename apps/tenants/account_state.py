"""Read-only admission and the bounded cancellation export window."""

from datetime import date, timedelta
from typing import Final

from django.core.exceptions import PermissionDenied
from django.utils.timezone import localdate

from apps.core.tenancy import MissingTenantContext, current_tenant_id
from apps.tenants.models import BillingAccount, BillingStatus, Tenant

EXPORT_WINDOW: Final = timedelta(days=90)
READ_ONLY_MESSAGE: Final = (
    "Esta conta está em modo somente leitura. A exportação dos dados continua "
    "disponível durante o prazo de acesso."
)


class TenantReadOnlyError(PermissionDenied):
    """The account admits reads and eligible exports, not new business writes."""


def export_until(tenant: Tenant) -> date | None:
    """Keep the last service day inclusive and grant ninety subsequent days."""
    if tenant.cancellation_ends_on is None:
        return None
    return tenant.cancellation_ends_on + EXPORT_WINDOW


def export_available(tenant: Tenant, *, on: date | None = None) -> bool:
    """Evaluate the contractual window independently of invoice delinquency."""
    deadline = export_until(tenant)
    return deadline is None or (on or localdate()) <= deadline


def read_only_reason(tenant: Tenant, *, on: date | None = None) -> str:
    """Read after the tenant GUC and before any portal-role switch."""
    if current_tenant_id.get() != tenant.pk:
        msg = "Account state needs the matching tenant context."
        raise MissingTenantContext(msg)
    today = on or localdate()
    if tenant.cancellation_ends_on is not None and today > tenant.cancellation_ends_on:
        return "cancelled"
    if BillingAccount.objects.filter(
        tenant=tenant, status=BillingStatus.SUSPENDED
    ).exists():
        return BillingStatus.SUSPENDED
    return ""


def require_writable(tenant: Tenant, *, on: date | None = None) -> None:
    """Recheck admission inside the domain operation's own lock boundary."""
    if read_only_reason(tenant, on=on):
        raise TenantReadOnlyError(READ_ONLY_MESSAGE)

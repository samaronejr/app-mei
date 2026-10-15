"""Resolve settings navigation and the private overdue banner only when enabled."""

from django import template
from django.http import HttpRequest
from django.urls import reverse

from apps.accounts.models import User
from apps.authz.firm_account import can_manage_account
from apps.core.flags import tenant_row_has
from apps.delivery.notice_authority import may_manage
from apps.tenants.models import BillingAccount, BillingStatus, Tenant

register = template.Library()


@register.simple_tag
def notice_policy_href(request: HttpRequest | None = None) -> str:
    """Both shells offer the private policy only to the ordinary current owner."""
    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if (
        not isinstance(tenant, Tenant)
        or not isinstance(user, User)
        or not may_manage(user, tenant)
    ):
        return ""
    return reverse("notice-policy")


@register.simple_tag
def firm_settings_context(request: HttpRequest | None = None) -> dict[str, bool]:
    """Keep flag-off and public shells free of new billing or membership queries."""
    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if (
        not isinstance(tenant, Tenant)
        or not isinstance(user, User)
        or not tenant_row_has(tenant, "firm_settings_r1")
    ):
        return {"enabled": False, "past_due": False}
    return {
        "enabled": True,
        "past_due": can_manage_account(user, tenant.pk)
        and BillingAccount.objects.filter(status=BillingStatus.PAST_DUE).exists(),
    }

"""Flag-gated nightly account metering, engagement expiry and summary enqueue."""

from datetime import date

from celery import shared_task
from django.utils.timezone import localdate

from apps.clients.offboarding import offboard_engagements
from apps.core.flags import tenant_has
from apps.core.tasks import PlatformTask
from apps.core.tenancy import tenant_context
from apps.tenants.account_state import read_only_reason
from apps.tenants.export_tasks import build_account_export, sweep_account_exports
from apps.tenants.models import Membership, NotificationPreference, Tenant
from apps.tenants.notification_tasks import dispatch_firm_outbox
from apps.tenants.notifications import enqueue_summary
from apps.tenants.usage import record_tenant_usage


@shared_task(base=PlatformTask, bind=True, ignore_result=True)
def record_usage(self: PlatformTask, *, on: date | None = None) -> int:
    """Record each enabled firm's daily measurements once."""
    day = on or localdate()
    count = 0
    for tenant_id in self.tenant_ids():
        if tenant_has(tenant_id, "firm_settings_r1"):
            count += record_tenant_usage(tenant_id, on=day)
    return count


@shared_task(base=PlatformTask, bind=True, ignore_result=True)
def offboard_expired_engagements(self: PlatformTask, *, on: date | None = None) -> int:
    """Apply the grace clock without exposing or removing retained evidence."""
    day = on or localdate()
    return sum(
        offboard_engagements(tenant_id, on=day)
        for tenant_id in self.tenant_ids()
        if tenant_has(tenant_id, "firm_settings_r1")
    )


@shared_task(base=PlatformTask, bind=True, ignore_result=True)
def queue_daily_summaries(self: PlatformTask, *, on: date | None = None) -> int:
    """Enqueue actual summaries for active firm members who explicitly opted in."""
    day = on or localdate()
    count = 0
    for tenant_id in self.tenant_ids():
        with tenant_context(tenant_id):
            if not tenant_has(tenant_id, "firm_settings_r1"):
                continue
            # PLATFORM_QUERY_OK: the trusted scheduler's current tenant only.
            tenant = Tenant.objects.get(pk=tenant_id)
            if read_only_reason(tenant):
                continue
            # PLATFORM_QUERY_OK: exact active firm seats, never portal identities.
            active_users = Membership.objects.filter(
                tenant=tenant, is_active=True, client__isnull=True
            ).values("user_id")
            for preference in NotificationPreference.objects.filter(
                daily_summary=True, user_id__in=active_users, user__is_active=True
            ).select_related("user"):
                enqueue_summary(preference.user, tenant, day)
                count += 1
    return count


__all__ = [
    "build_account_export",
    "dispatch_firm_outbox",
    "offboard_expired_engagements",
    "queue_daily_summaries",
    "record_usage",
    "sweep_account_exports",
]

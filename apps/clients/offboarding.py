"""Expire engagement access with an injected clock while retaining all evidence."""

from datetime import date, timedelta
from uuid import UUID

from django.conf import settings
from django.db.models import Count, Q
from django.utils.timezone import localdate

from apps.audit.services import ObjectRef, record_event
from apps.clients.models import ClientCompany, Engagement
from apps.core.flags import tenant_has
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleState, ServiceCycle
from apps.cycles.services.cycle import OPEN_STATES, emit
from apps.tenants.authority import lock_users
from apps.tenants.models import Membership


def portal_engagement_current(client_id: UUID, *, on: date | None = None) -> bool:
    """Keep legacy clients without engagements, but never extend an expired grant."""
    cutoff = (on or localdate()) - timedelta(
        days=settings.ENGAGEMENT_OFFBOARDING_GRACE_DAYS
    )
    counts = Engagement.objects.filter(client_id=client_id).aggregate(
        total=Count("pk"),
        current=Count("pk", filter=Q(ends_on__isnull=True) | Q(ends_on__gt=cutoff)),
    )
    return bool(counts["total"] == 0 or counts["current"] > 0)


def offboard_engagements(tenant_id: UUID, *, on: date | None = None) -> int:
    """Cancel expired open work and revoke seats only after every engagement ends."""
    today = on or localdate()
    cutoff = today - timedelta(days=settings.ENGAGEMENT_OFFBOARDING_GRACE_DAYS)
    with tenant_context(tenant_id):
        if not tenant_has(tenant_id, "firm_settings_r1"):
            return 0
        client_ids = list(
            Engagement.objects.filter(ends_on__lte=cutoff)
            .values_list("client_id", flat=True)
            .distinct()
        )
    changed = 0
    for client_id in client_ids:
        with tenant_context(tenant_id):
            users = list(
                # PLATFORM_QUERY_OK: trusted lifecycle job, exact tenant/client seats.
                Membership.objects.filter(
                    tenant_id=tenant_id, client_id=client_id, is_active=True
                ).values_list("user_id", flat=True)
            )
            lock_users(users)
            ClientCompany.objects.select_for_update(no_key=True).get(pk=client_id)
            expired = list(
                Engagement.objects.select_for_update()
                .filter(client_id=client_id, ends_on__lte=cutoff)
                .values_list("pk", flat=True)
            )
            cancelled = 0
            for cycle in (
                ServiceCycle.objects.select_for_update(no_key=True)
                .filter(
                    client_id=client_id,
                    engagement_id__in=expired,
                    state__in=OPEN_STATES,
                )
                .order_by("pk")
            ):
                cycle.state = CycleState.CANCELLED
                cycle.version += 1
                cycle.save(update_fields=["state", "version"])
                emit(cycle, None, "cycle.offboarded", payload={"on": today.isoformat()})
                cancelled += 1
            revoked = 0
            if not portal_engagement_current(client_id, on=today):
                # PLATFORM_QUERY_OK: deactivate only this client's surviving seats.
                revoked = Membership.objects.filter(
                    tenant_id=tenant_id, client_id=client_id, is_active=True
                ).update(is_active=False)
            if cancelled or revoked:
                record_event(
                    action="engagement.offboarded",
                    obj=ObjectRef("ClientCompany", str(client_id)),
                    metadata={
                        "on": today.isoformat(),
                        "cycles": cancelled,
                        "memberships": revoked,
                    },
                )
                changed += 1
    return changed

"""Schedule cancellation without erasure, then reuse class-aware retention."""

from datetime import date, datetime
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.timezone import localdate

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.firm_account import require_account_owner
from apps.core.flags import tenant_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.cycles.retention_records import ContentPurgeClaim
from apps.cycles.retention_tasks import purge_tenant
from apps.tenants.account_state import EXPORT_WINDOW, export_available, require_writable
from apps.tenants.models import Tenant
from apps.tenants.services import check_settings_version, lock_firm_settings


@transaction.atomic
def schedule_cancellation(
    actor: User,
    tenant_id: UUID,
    *,
    ends_on: date,
    expected_version: int,
    on: date | None = None,
) -> Tenant:
    """Record the owner's end date; the account and its evidence stay present."""
    fresh, tenant = lock_firm_settings(actor, tenant_id)
    require_account_owner(fresh, tenant_id)
    require_writable(tenant, on=on)
    check_settings_version(tenant.settings_version, expected_version)
    if ends_on < (on or localdate()) or ends_on > date.max - EXPORT_WINDOW:
        msg = "Informe uma data futura válida para encerrar e exportar a conta."
        raise ValidationError(msg, code="cancellation_date")
    if ContentPurgeClaim.objects.filter(completed_at=None).exists():
        msg = (
            "Há uma remoção de conteúdo em andamento. "
            "Aguarde a conclusão antes de agendar o encerramento."
        )
        raise ValidationError(msg, code="cancellation_purge_pending")
    tenant.cancellation_ends_on = ends_on
    tenant.settings_version += 1
    tenant.save(
        update_fields=["cancellation_ends_on", "settings_version", "updated_at"]
    )
    record_event(
        action="tenant.cancellation_scheduled",
        actor=fresh,
        obj=ObjectRef("Tenant", str(tenant_id)),
        metadata={"ends_on": ends_on.isoformat(), "version": tenant.settings_version},
    )
    return tenant


def purge_cancelled_tenant(tenant_id: UUID, *, now: datetime) -> dict[str, int]:
    """Use the same hold/class decision after the export window, dry-run by default."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        # PLATFORM_QUERY_OK: inspect this explicit lifecycle job's tenant only.
        tenant = Tenant.objects.filter(pk=tenant_id, is_active=True).first()
        if (
            tenant is None
            or not tenant_has(tenant_id, "firm_settings_r1")
            or tenant.cancellation_ends_on is None
            or export_available(tenant, on=localdate(now))
        ):
            return {"eligible": 0, "held": 0, "retained": 0, "purged": 0, "failed": 0}
    return purge_tenant(tenant_id, dry_run=settings.RETENTION_PURGE_DRY_RUN, now=now)

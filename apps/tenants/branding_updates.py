"""Small logo uploads commit their intent before touching object storage."""

from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.audit.services import ObjectRef, record_event
from apps.authz.services import can
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.tenants.account_state import read_only_reason
from apps.tenants.authority import lock_users
from apps.tenants.models import Tenant


def store_logo(
    tenant_id: UUID, actor_id: UUID, request_id: UUID, contents: bytes
) -> None:
    """Upload one owned key and publish only a still-authorized current attempt."""
    assert_no_transaction()
    prefix = f"branding/{tenant_id}/{request_id}/"
    key = f"{prefix}logo.png"
    saved = ""
    error_code = ""
    try:
        saved = default_storage.save(key, ContentFile(contents))
        if not saved.startswith(prefix):
            error_code = "storage_identity"
    except (OSError, BotoCoreError, ClientError):
        error_code = "storage_failed"
    published = False
    with tenant_context(tenant_id):
        actor = lock_users((actor_id,)).get(actor_id)
        # PLATFORM_QUERY_OK: finalize only the persisted upload token of this firm.
        tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id)
        if tenant.logo_request_id == request_id:
            authorized = (
                actor is not None
                and actor.is_active
                and can(actor, "billing.manage", tenant_id=tenant_id)
                and not read_only_reason(tenant)
            )
            if not authorized:
                error_code = "authorization_changed"
            if not error_code:
                tenant.logo = saved
                published = True
            tenant.logo_upload_pending = False
            tenant.logo_error_code = error_code
            tenant.settings_version += 1
            tenant.save(
                update_fields=[
                    "logo",
                    "logo_upload_pending",
                    "logo_error_code",
                    "settings_version",
                    "updated_at",
                ]
            )
            record_event(
                action="tenant.logo_updated" if published else "tenant.logo_failed",
                actor=actor,
                obj=ObjectRef("Tenant", str(tenant_id)),
                metadata={"request_id": str(request_id), "error_code": error_code},
            )
    if not published and saved.startswith(prefix):
        assert_no_transaction()
        default_storage.delete(saved)

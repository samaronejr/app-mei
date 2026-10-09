"""Versioned firm settings and each member's notification preference."""

from dataclasses import dataclass
from functools import partial
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import ObjectRef, record_event
from apps.authz.services import can
from apps.authz.support import current_support
from apps.core.flags import tenant_has
from apps.core.tenancy import current_tenant_id
from apps.tenants.account_state import require_writable
from apps.tenants.authority import lock_users
from apps.tenants.branding import validate_logo
from apps.tenants.branding_updates import store_logo
from apps.tenants.models import Membership, NotificationPreference, Tenant
from apps.tenants.support_models import SupportGrant, SupportScope


class TenantConflictError(Exception):
    """A rendered settings revision is stale; no change was written."""


def check_settings_version(actual: int, expected: int) -> None:
    """Refuse a stale form before any row or event is changed."""
    if actual != expected:
        msg = (
            "As configurações mudaram depois que você abriu a página. "
            "Nada foi salvo. Recarregue antes de tentar novamente."
        )
        raise TenantConflictError(msg)


def lock_firm_settings(actor: User, tenant_id: UUID) -> tuple[User, Tenant]:
    """Follow the shared user-before-tenant authority order inside a transaction."""
    if current_tenant_id.get() != tenant_id:
        raise Http404
    fresh = lock_users((actor.pk,)).get(actor.pk)
    if fresh is None or not fresh.is_active:
        raise PermissionDenied
    support = current_support.get()
    if support is not None:
        # The read_write_settings scope buys exactly this write: the grant IS the
        # membership substitute here. Hold the exact admitted grant's row
        # through commit, using the same fence as owner revocation: a revoke
        # either commits first and refuses this write, or follows its commit.
        live = (
            SupportGrant.objects.select_for_update(no_key=True)
            .filter(
                pk=support.grant_id,
                tenant_id=tenant_id,
                requested_by=fresh,
            )
            .first()
        )
        if live is not None and live.scope == SupportScope.READ_WRITE_SETTINGS:
            tenant = (
                # PLATFORM_QUERY_OK: the exact locked grant row names
                # exactly this tenant for this actor; a support session has no
                # membership row, so there is no portfolio to scope through.
                Tenant.objects.select_for_update(no_key=True)
                .filter(pk=tenant_id, is_active=True)
                .first()
            )
            # Time can advance while either fence is acquired. Check the window
            # after the locks, not against the middleware's admitted snapshot.
            if not live.is_active():
                tenant = None
        else:
            tenant = None
    else:
        tenant = (
            Tenant.objects.for_user(fresh)
            .select_for_update(no_key=True)
            .filter(pk=tenant_id, is_active=True)
            .first()
        )
    if tenant is None or not tenant_has(tenant_id, "firm_settings_r1"):
        raise Http404
    return fresh, tenant


@dataclass(frozen=True, slots=True)
class FirmProfile:
    """The profile fields offered by the firm settings form."""

    name: str
    brand_name: str = ""
    brand_color: str = ""
    logo: bytes | None = None
    remove_logo: bool = False


@transaction.atomic
def save_profile(
    actor: User, tenant_id: UUID, profile: FirmProfile, *, expected_version: int
) -> Tenant:
    """Bind the independent actor, firm, profile and rendered revision."""
    fresh, tenant = lock_firm_settings(actor, tenant_id)
    if not can(fresh, "billing.manage", tenant_id=tenant_id):
        raise PermissionDenied
    require_writable(tenant)
    check_settings_version(tenant.settings_version, expected_version)
    if profile.logo is not None and profile.remove_logo:
        msg = "Escolha entre enviar um logotipo ou remover o atual."
        raise ValidationError(msg)
    tenant.name = profile.name.strip()
    tenant.brand_name = profile.brand_name.strip()
    tenant.brand_color = profile.brand_color.strip()
    tenant.full_clean(exclude=["logo"], validate_unique=False)
    fields = [
        "name",
        "brand_name",
        "brand_color",
        "settings_version",
        "updated_at",
    ]
    if profile.logo is not None or profile.remove_logo:
        tenant.logo_request_id = uuid4()
        tenant.logo_upload_pending = profile.logo is not None
        tenant.logo_error_code = ""
        fields.extend(["logo_request_id", "logo_upload_pending", "logo_error_code"])
        if profile.remove_logo:
            tenant.logo = ""
            fields.append("logo")
        elif profile.logo is not None:
            with ContentFile(profile.logo, name="logo.png") as upload:
                validate_logo(upload)
            transaction.on_commit(
                partial(
                    store_logo,
                    tenant_id,
                    fresh.pk,
                    tenant.logo_request_id,
                    profile.logo,
                )
            )
    tenant.settings_version += 1
    tenant.save(update_fields=fields)
    record_event(
        action="tenant.profile_updated",
        actor=fresh,
        obj=ObjectRef("Tenant", str(tenant_id)),
        metadata={"version": tenant.settings_version, "fields": fields},
    )
    return tenant


@transaction.atomic
def save_notification_preference(
    actor: User, tenant_id: UUID, *, enabled: bool, expected_version: int
) -> NotificationPreference:
    """Change only this member's opt-in; sending belongs to the durable outbox."""
    fresh, tenant = lock_firm_settings(actor, tenant_id)
    require_writable(tenant)
    if not can(fresh, "clients.view_assigned", tenant_id=tenant_id):
        raise PermissionDenied
    # PLATFORM_QUERY_OK: an exact, active firm seat is the preference's authority.
    if not Membership.objects.filter(
        tenant=tenant, user=fresh, is_active=True, client__isnull=True
    ).exists():
        raise PermissionDenied
    preference = (
        NotificationPreference.objects.select_for_update().filter(user=fresh).first()
    )
    check_settings_version(preference.version if preference else 0, expected_version)
    if preference is None:
        preference = NotificationPreference(tenant=tenant, user=fresh)
    else:
        preference.version += 1
    preference.daily_summary = enabled
    preference.updated_at = timezone.now()
    preference.save()
    record_event(
        action="tenant.notification_preference_updated",
        actor=fresh,
        obj=ObjectRef("NotificationPreference", str(preference.pk)),
        metadata={"enabled": enabled, "version": preference.version},
    )
    return preference

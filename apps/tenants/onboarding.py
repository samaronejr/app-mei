"""Audited platform onboarding and the manual subscription maintenance path."""

from dataclasses import dataclass
from datetime import date
from functools import partial
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.core.validators import validate_email
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import Origin, record_platform_event
from apps.authz.services import can
from apps.core.tenancy import tenant_context
from apps.tenants.authority import lock_users
from apps.tenants.models import BillingAccount, Invite, Tenant, TenantRole
from apps.tenants.notifications import enqueue_owner_invite
from apps.tenants.services import check_settings_version

DEFAULT_FIRM_FEATURES = ("cycles_r1", "import_r1", "firm_settings_r1")


def _operator(actor: User) -> User:
    fresh = lock_users((actor.pk,)).get(actor.pk)
    if (
        fresh is None
        or not fresh.is_active
        or not fresh.is_staff
        or not can(fresh, "tenants.create")
        or not can(fresh, "billing.manage")
    ):
        raise PermissionDenied
    return fresh


@dataclass(frozen=True, slots=True)
class FirmRegistration:
    """The operator's explicit onboarding choices, without public price hypotheses."""

    name: str
    slug: str
    owner_email: str
    plan_band: str = ""
    active_client_limit: int | None = None


@transaction.atomic
def create_firm(actor: User, registration: FirmRegistration) -> Tenant:
    """Create firm, manual account and owner invitation in one audited operation."""
    fresh = _operator(actor)
    email = registration.owner_email.strip().lower()
    validate_email(email)
    tenant = Tenant(
        name=registration.name.strip(),
        slug=registration.slug,
        active_client_limit=registration.active_client_limit,
        features=list(DEFAULT_FIRM_FEATURES),
    )
    tenant.full_clean()
    tenant.save(force_insert=True)
    with tenant_context(tenant.pk):
        account = BillingAccount(tenant=tenant, plan_band=registration.plan_band)
        account.full_clean()
        account.save(force_insert=True)
        invite, _raw_token = Invite.issue(
            tenant=tenant, email=email, role=TenantRole.OWNER
        )
        enqueue_owner_invite(fresh, invite)
    transaction.on_commit(
        partial(
            record_platform_event,
            action=AuditAction.TENANT_UPDATED,
            origin=Origin(actor=fresh, subject=str(tenant.pk)),
            tenant_id=tenant.pk,
            metadata={
                "created": True,
                "flow": "new_firm",
                "invite_id": str(invite.pk),
                "features": list(DEFAULT_FIRM_FEATURES),
                "active_client_limit": tenant.active_client_limit,
            },
        )
    )
    return tenant


@dataclass(frozen=True, slots=True)
class BillingChange:
    """Only subscription metadata; payment credentials have no representation."""

    plan_band: str
    status: str
    next_invoice_on: date | None
    active_client_limit: int | None
    expected_version: int


@transaction.atomic
def update_billing(
    actor: User, tenant_id: UUID, change: BillingChange
) -> BillingAccount:
    """Let the platform operator suspend or restore, including a read-only firm."""
    fresh = _operator(actor)
    # PLATFORM_QUERY_OK: the explicitly authorized platform billing operation.
    tenant = Tenant.objects.select_for_update(no_key=True).filter(pk=tenant_id).first()
    if tenant is None:
        raise Http404
    with tenant_context(tenant_id):
        account = (
            BillingAccount.objects.select_for_update().filter(tenant=tenant).first()
        )
        check_settings_version(
            account.version if account else 0, change.expected_version
        )
        if account is None:
            account = BillingAccount(tenant=tenant)
        else:
            account.version += 1
        account.plan_band = change.plan_band.strip()
        account.status = change.status
        account.next_invoice_on = change.next_invoice_on
        account.updated_at = timezone.now()
        account.full_clean()
        tenant.active_client_limit = change.active_client_limit
        tenant.full_clean(exclude=["logo"], validate_unique=False)
        tenant.settings_version += 1
        tenant.save(
            update_fields=["active_client_limit", "settings_version", "updated_at"]
        )
        account.save()
    transaction.on_commit(
        partial(
            record_platform_event,
            action="tenant.billing_updated",
            origin=Origin(actor=fresh, subject=str(tenant.pk)),
            tenant_id=tenant.pk,
            metadata={
                "status": account.status,
                "version": account.version,
                "active_client_limit": tenant.active_client_limit,
            },
        )
    )
    return account

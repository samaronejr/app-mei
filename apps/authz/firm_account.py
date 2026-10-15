"""An account owner's authority is not the platform's administrative bypass."""

from typing import Final
from uuid import UUID

from django.core.exceptions import PermissionDenied

from apps.authz.services import Actor, can
from apps.tenants.models import Invite, Membership, TenantRole

ACCOUNT_CAPABILITY: Final = "tenants.account"


def is_owner_invite(invite: Invite) -> bool:
    """Distinguish the firm-owner grant from portal and ordinary staff invitations."""
    return invite.client_id is None and invite.role == TenantRole.OWNER


def can_manage_account(user: Actor, tenant_id: UUID) -> bool:
    """Require both the data-driven capability and this firm's active owner seat."""
    if not user.is_active or not can(user, ACCOUNT_CAPABILITY, tenant_id=tenant_id):
        return False
    # PLATFORM_QUERY_OK: this is the authorization lookup itself, bound to the
    # exact caller, firm and owner seat; platform privilege alone is insufficient.
    return Membership.objects.filter(
        tenant_id=tenant_id,
        user=user,
        client__isnull=True,
        is_active=True,
        role=TenantRole.OWNER,
    ).exists()


def require_account_owner(user: Actor, tenant_id: UUID) -> None:
    """Keep support/platform reach from substituting for an owner's account action."""
    if not can_manage_account(user, tenant_id):
        msg = "Somente o responsável pela conta pode realizar esta ação."
        raise PermissionDenied(msg)

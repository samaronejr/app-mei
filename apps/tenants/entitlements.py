"""Serialize admissions to the firm's active-client allowance."""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import Http404

from apps.clients.models import ClientCompany, ClientStatus
from apps.core.tenancy import current_tenant_id
from apps.tenants.account_state import require_writable
from apps.tenants.models import Tenant

ACTIVE_CLIENT_STATES: Final = frozenset({ClientStatus.ONBOARDING, ClientStatus.ACTIVE})


class EntitlementExceededError(ValidationError):
    """The proposed new active clients do not fit the current contracted limit."""


@dataclass(frozen=True, slots=True)
class ClientAllowance:
    """Current usage and a proposed admission; NULL and zero remain distinct."""

    active: int
    limit: int | None
    additional: int

    @property
    def remaining(self) -> int | None:
        """Return unoccupied slots, or None for an unlimited contract."""
        return None if self.limit is None else max(0, self.limit - self.active)

    @property
    def allowed(self) -> bool:
        """A duplicate-only operation does not need a new slot."""
        return (
            self.additional == 0
            or self.limit is None
            or self.active + self.additional <= self.limit
        )


def active_client_count() -> int:
    """Count onboarding as active so a fresh registration cannot evade its ceiling."""
    return ClientCompany.objects.filter(status__in=ACTIVE_CLIENT_STATES).count()


def lock_entitlement(tenant_id: UUID) -> Tenant:
    """Take the quota fence before any client or import-batch lock."""
    if current_tenant_id.get() != tenant_id:
        raise Http404
    tenant = (
        # PLATFORM_QUERY_OK: quota writers serialize the exact ambient tenant.
        Tenant.objects.select_for_update(no_key=True)
        .filter(pk=tenant_id, is_active=True)
        .first()
    )
    if tenant is None:
        raise Http404
    require_writable(tenant)
    return tenant


def client_allowance(tenant: Tenant, *, additional: int = 0) -> ClientAllowance:
    """Build a live preview from the same count used by the locked admission."""
    if current_tenant_id.get() != tenant.pk:
        raise Http404
    return ClientAllowance(
        active_client_count(), tenant.active_client_limit, additional
    )


def require_client_capacity(tenant: Tenant, *, additional: int = 1) -> None:
    """Soft-block admission without locking the account or exposing portfolio counts."""
    if additional == 0 or tenant.active_client_limit is None:
        return
    if not client_allowance(tenant, additional=additional).allowed:
        msg = (
            "O limite de clientes ativos desta conta foi atingido. "
            "Revise a importação ou fale com o responsável pela conta."
        )
        raise EntitlementExceededError(msg, code="active_client_limit")

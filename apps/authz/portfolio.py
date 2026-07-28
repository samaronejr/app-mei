"""How wide a book of business an account works over.

This is a **narrowing** rule layered under the permission matrix, and the distinction
matters. The published matrix grants `clients.view_all` to the staff accountant as
well as to the firm owner, so on capability alone a staff accountant could be shown
every client the firm keeps books for. The product does not do that: assigning clients
to an accountant is the act that defines their book, and their queues, dashboard and
counts are that book and nothing else.

Showing **less** than the matrix permits breaks no invariant. Showing more would. So
both conditions must hold before the scope widens:

* the account must hold `clients.view_all` — the ceiling, read from the matrix, which
  can never be widened from here;
* and its role must be one that carries the whole firm's portfolio by definition.

`test_no_firm_wide_role_exceeds_the_matrix` asserts the second set is a subset of the
first, so this file can only ever narrow what the matrix already allows.

The role comparison lives here rather than at the call site because the role-check
guard requires exactly that: a comparison anywhere else would be a second, invisible
copy of the matrix.
"""

from enum import StrEnum
from typing import Final
from uuid import UUID

from django.db.models import QuerySet

from apps.accounts.models import User
from apps.authz.services import Actor, can, role_of
from apps.clients.models import ClientCompany
from apps.tenants.models import TenantRole

VIEW_ASSIGNED: Final = "clients.view_assigned"
VIEW_ALL: Final = "clients.view_all"

# The roles whose job IS the whole firm's portfolio. A staff accountant is deliberately
# absent: they work the clients they were assigned, and an accountant whose assignments
# were revoked must end up with an empty queue rather than with the entire firm.
FIRM_WIDE_ROLES: Final[frozenset[str]] = frozenset(
    {TenantRole.OWNER, TenantRole.OPERATIONS_ADMIN},
)


class PortfolioScope(StrEnum):
    """Which clients an account's queues and counts are computed over."""

    NONE = "none"
    ASSIGNED = "assigned"
    ALL = "all"


def portfolio_scope(user: Actor, *, tenant_id: UUID | None = None) -> PortfolioScope:
    """Report how wide this account's portfolio is in the firm it is working in."""
    if not can(user, VIEW_ASSIGNED, tenant_id=tenant_id):
        return PortfolioScope.NONE
    if not can(user, VIEW_ALL, tenant_id=tenant_id):
        return PortfolioScope.ASSIGNED
    role = role_of(user, tenant_id) if isinstance(user, User) else None
    if role in FIRM_WIDE_ROLES:
        return PortfolioScope.ALL
    return PortfolioScope.ASSIGNED


def visible_clients(
    user: Actor,
    *,
    tenant_id: UUID | None = None,
) -> QuerySet[ClientCompany]:
    """Return the clients this account's screens are computed over.

    Always a queryset, never `None` and never a list: every caller composes further
    filters onto it, and the tenant filter comes from the manager rather than from
    anything written here.
    """
    scope = portfolio_scope(user, tenant_id=tenant_id)
    if scope is PortfolioScope.NONE:
        return ClientCompany.objects.none()
    if scope is PortfolioScope.ASSIGNED:
        return ClientCompany.objects.assigned_to(user)  # type: ignore[arg-type]
    return ClientCompany.objects.all()


__all__ = [
    "FIRM_WIDE_ROLES",
    "VIEW_ALL",
    "VIEW_ASSIGNED",
    "PortfolioScope",
    "portfolio_scope",
    "visible_clients",
]

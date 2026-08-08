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
from apps.authz.models import GrantLevel
from apps.authz.services import Actor, granted_levels, role_of
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
    """Report how wide this account's portfolio is in the firm it is working in.

    The two capabilities are resolved in ONE round trip rather than by asking `can()`
    twice, which is worth three of the registry's queries and none of its freshness.
    Nothing is cached and nothing is shared: `require_can` and the template's
    `{% can %}` still ask the database on their own, exactly as
    `tests/ui/test_clients_pages.py:127-135` requires — that comment records
    memoising the answer ACROSS those three answerers as rejected, because this is a
    screen where a stale authorization answer is a visible cross-tenant leak. Batching
    two questions inside this one answer is a different act: the answer is still read
    fresh, on this call, and it is still this function's own.

    `granted_levels` is the same resolution `resolve_level` performs — same order, same
    superuser short-circuit, same missing-grant default, pinned by
    `test_bulk_resolution_agrees_with_resolve_level` — so this is not a second policy
    source. `full` is compared explicitly because `can()` with no object accepts only
    `full`: `limited` and `own` need an object they are not given here, and the three
    channel levels are refusals. Treating any non-`none` level as a yes would widen a
    portfolio past its ceiling, which is the one direction this module may never move.
    """
    levels = granted_levels(user, (VIEW_ASSIGNED, VIEW_ALL), tenant_id=tenant_id)
    if levels[VIEW_ASSIGNED] != GrantLevel.FULL:
        return PortfolioScope.NONE
    if levels[VIEW_ALL] != GrantLevel.FULL:
        return PortfolioScope.ASSIGNED
    # Asked separately because the bulk resolver reports levels, not the role behind
    # them. Reading it back out of `granted_levels` would mean changing a signature in
    # `apps/authz/services.py`, and the role is only needed on the branch where both
    # capabilities already said yes — so it stays one query on the widest path only.
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
    if scope is PortfolioScope.ALL:
        return ClientCompany.objects.all()
    # An assignment is a row keyed on a user id, so an actor that is not a `User` holds
    # none by construction — which is why `assigned_to` keeps its `User` parameter
    # instead of widening to accept something it could never match. `portfolio_scope`
    # already answers NONE for such an actor, so naming the case here resolves it to
    # the same empty queryset rather than adding a behaviour.
    if scope is PortfolioScope.ASSIGNED and isinstance(user, User):
        return ClientCompany.objects.assigned_to(user)
    return ClientCompany.objects.none()


__all__ = [
    "FIRM_WIDE_ROLES",
    "VIEW_ALL",
    "VIEW_ASSIGNED",
    "PortfolioScope",
    "portfolio_scope",
    "visible_clients",
]

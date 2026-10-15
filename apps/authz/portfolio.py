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

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.accounts.models import User
from apps.authz.models import GrantLevel, RoleGrant, UserCapabilityGrant
from apps.authz.services import Actor, granted_levels_with_role
from apps.authz.stash import portal_stash
from apps.authz.support import in_support_mode, support_clients
from apps.clients.models import ClientAssignment, ClientCompany
from apps.core.tenancy import current_client_id
from apps.tenants.models import Membership, TenantRole

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from django.db.models import QuerySet

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

    The two capabilities and the active role are resolved together rather than by
    asking `can()` twice and then reading the role again. The registry's original
    seven-query decision is now three without giving up freshness.

    On firm screens, nothing is cached or shared across answerers: `require_can`,
    navigation and the template's `{% can %}` still resolve independently, exactly as
    `test_each_firm_screen_answerer_resolves_authorization_for_itself` requires. The
    identity-and-tenant-bound portal stash remains the deliberate exception because
    `app_portal` cannot read the authorization tables. Returning two capabilities and
    one role from this function's own live resolution does not cross that boundary.

    `granted_levels_with_role` uses the same resolution `resolve_level` performs — same
    order, same superuser short-circuit, same missing-grant default, pinned by
    `test_bulk_resolution_agrees_with_resolve_level` — so this is not a second policy
    source. `full` is compared explicitly because `can()` with no object accepts only
    `full`: `limited` and `own` need an object they are not given here, and the three
    channel levels are refusals. Treating any non-`none` level as a yes would widen a
    portfolio past its ceiling, which is the one direction this module may never move.
    """
    role, levels = granted_levels_with_role(
        user,
        (VIEW_ASSIGNED, VIEW_ALL),
        tenant_id=tenant_id,
    )
    return scope_for_levels(role, levels)


def scope_for_levels(role: str | None, levels: Mapping[str, str]) -> PortfolioScope:
    """Narrow a live resolution using the same two portfolio ceilings."""
    if levels[VIEW_ASSIGNED] != GrantLevel.FULL:
        return PortfolioScope.NONE
    if levels[VIEW_ALL] != GrantLevel.FULL:
        return PortfolioScope.ASSIGNED
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
    # A support session's book is whatever the owner approved, intersected with
    # the tenant scope the manager already enforces. Empty grant client_ids means
    # firm-level metadata only, so the answer is `.none()`, never `.all()`.
    return clients_in_scope(user, portfolio_scope(user, tenant_id=tenant_id))


def clients_in_scope(user: Actor, scope: PortfolioScope) -> QuerySet[ClientCompany]:
    """Compose a resolved portfolio, retaining the support grant intersection."""
    granted = support_clients()
    if granted is not None:
        return granted
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


def firm_wide_memberships(memberships: QuerySet[Membership]) -> QuerySet[Membership]:
    """Select roster members whose live portfolio_scope would answer ALL.

    This is a display roster, not an actor authorization cache. Keep the role
    narrowing here and intersect it with both matrix ceilings, including live
    raises. Support/portal resolution has no firm role, so cannot answer ALL.
    """
    if (
        in_support_mode()
        or portal_stash.get() is not None
        or current_client_id.get() is not None
    ):
        return memberships.none()
    rows = memberships.filter(
        is_active=True, client__isnull=True, role__in=FIRM_WIDE_ROLES
    )
    for index, action in enumerate((VIEW_ASSIGNED, VIEW_ALL)):
        # PLATFORM_QUERY_OK: the product-wide matrix, keyed by this member's role.
        role_grants = RoleGrant.objects.filter(
            role=OuterRef("role"), capability__slug=action, level=GrantLevel.FULL
        )
        raises = UserCapabilityGrant.objects.filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
            tenant_id=OuterRef("tenant_id"),
            user_id=OuterRef("user_id"),
            capability__slug=action,
            level=GrantLevel.FULL,
            revoked_at__isnull=True,
        )
        name = f"portfolio_full_{index}"
        rows = rows.annotate(**{name: Exists(role_grants) | Exists(raises)}).filter(
            Q(user__is_superuser=True) | Q(**{name: True})
        )
    return rows


def assignable_members(
    client_id: UUID | OuterRef, *, tenant_id: UUID
) -> QuerySet[Membership]:
    """Batch the exception roster without widening any member's portfolio.

    An assignment admits an active firm seat exactly as the old roster did.
    Otherwise both FULL ceilings and a firm-wide role are required. Support and
    portal resolutions have no firm role, so only the assigned branch survives.
    An aggregate caller uses OuterRef(OuterRef("pk")) for the enclosing client.
    """
    assigned = ClientAssignment.objects.filter(
        tenant_id=tenant_id, client_id=client_id, user_id=OuterRef("user_id")
    )
    # PLATFORM_QUERY_OK: active firm seats for this one explicitly named tenant.
    members = Membership.objects.filter(
        tenant_id=tenant_id, client__isnull=True, is_active=True
    )
    return (
        members.filter(
            Q(Exists(assigned)) | Q(pk__in=firm_wide_memberships(members).values("pk"))
        )
        .select_related("user")
        .order_by("user__email")
    )


__all__ = [
    "FIRM_WIDE_ROLES",
    "VIEW_ALL",
    "VIEW_ASSIGNED",
    "PortfolioScope",
    "assignable_members",
    "clients_in_scope",
    "firm_wide_memberships",
    "portfolio_scope",
    "scope_for_levels",
    "visible_clients",
]

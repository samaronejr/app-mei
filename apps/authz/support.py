"""Support sessions: the grant-resolved capability map and request binding.

A support session is a *different authorization identity*, not a widening of the
platform account's. The account has no firm membership by construction — if it
held one it would be an ordinary member request, not support — so the matrix
alone would answer NONE to every capability and the session would see nothing
at all. `current_support` supplies the answers the owner's decision earned:

* the read capabilities below resolve FULL, matching what a read-only visitor
  needs to walk queues, client pages and the audit surface;
* `billing.manage` resolves FULL only when the owner chose
  `read_write_settings` — the one write a support session may perform, and the
  service still re-checks the live grant inside its own lock;
* everything else — approvals, sends, transfers, downloads, imports, invites,
  billing records, account actions — resolves NONE. `can()` then fails closed
  at the authorization layer, not merely in the UI.

The stash lives in a ContextVar for exactly the length of the request, mirroring
the flag cache. The grant row itself is re-read from the database on every
request (`resolve_support_grant` in the middleware) — never from the stash and
never from the session — so a revocation or expiry takes effect on the very next
request, mid-session or not.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.tenants.support_models import SupportGrant, SupportScope

# Read-only firm surfaces a support session may answer FULL for. Every write
# capability is absent on purpose; the grant's scope adds billing.manage only.
SUPPORT_READ_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "clients.view_assigned",
        "clients.view_all",
        "cycles.review",
        "audit.view",
    }
)
SETTINGS_WRITE_CAPABILITY: Final = "billing.manage"


@dataclass(frozen=True, slots=True)
class SupportContext:
    """The live grant's decision, bound to one request."""

    grant_id: UUID
    tenant_id: UUID
    scope: str
    client_ids: frozenset[str]
    expires_at: object


current_support: ContextVar[SupportContext | None] = ContextVar(
    "current_support",
    default=None,
)


def in_support_mode() -> bool:
    """Report whether this request is inside an active support session."""
    return current_support.get() is not None


def support_scope() -> str | None:
    """Return the grant's scope, or None outside a support session."""
    context = current_support.get()
    return context.scope if context is not None else None


def resolve_support_grant(
    user_id: UUID,
    tenant_id: UUID,
    *,
    at: datetime | None = None,
) -> SupportGrant | None:
    """Return the caller's live grant for this firm, or None.

    Resolved from the database on every call — there is no session or cache copy
    of grant state by design. The read wraps itself in `tenant_context` so it is
    self-sufficient both inside a request transaction (a savepoint: the GUC is
    already set to this tenant and re-setting it is a no-op) and before one
    exists (autocommit: the `SET LOCAL` lives for exactly this read and is
    discarded with it). `objects` is the tenant-scoped manager, so the RLS
    predicate is what answers — the read can never reach across firms.
    """
    from apps.core.tenancy import tenant_context  # noqa: PLC0415

    moment = at or timezone.now()
    with tenant_context(tenant_id):
        return (
            SupportGrant.objects.filter(
                tenant_id=tenant_id,
                requested_by_id=user_id,
                approved_by_owner__isnull=False,
                revoked_at__isnull=True,
                starts_at__lte=moment,
                expires_at__gt=moment,
            )
            .order_by("-created_at")
            .first()
        )


def has_active_support_grant(user_id: UUID, tenant_id: UUID) -> bool:
    """Answer the boolean the admin break-glass path asks, per request."""
    return resolve_support_grant(user_id, tenant_id) is not None


def support_clients() -> QuerySet[ClientCompany] | None:
    """Return the grant-limited client queryset, or None outside support mode.

    An EMPTY `client_ids` grant means firm-level metadata only — the queryset is
    deliberately `.none()`, not `.all()`. Listed ids intersect with the normal
    tenant scope: the manager still answers inside `tenant_context`, and an id
    naming a client outside the grant is treated as outside the portfolio —
    which `load_visible_client` renders as a 404, not a 403.
    """
    context = current_support.get()
    if context is None:
        return None
    if not context.client_ids:
        return ClientCompany.objects.none()
    return ClientCompany.objects.filter(pk__in=list(context.client_ids))


def support_level(action: str) -> str | None:
    """Answer the support session's level for `action`, or None to defer.

    Returning None leaves `resolve_level` to its normal path, which — for the
    platform account support actors are — resolves NONE anyway. Returning an
    explicit answer for the read set is what makes the session able to see the
    surfaces the owner intended, and returning NONE for the rest is what keeps
    `can()` fail-closed for every write the plan forbids.
    """
    context = current_support.get()
    if context is None:
        return None
    from apps.authz.models import GrantLevel  # noqa: PLC0415

    if action in SUPPORT_READ_CAPABILITIES:
        return GrantLevel.FULL
    if (
        action == SETTINGS_WRITE_CAPABILITY
        and context.scope == SupportScope.READ_WRITE_SETTINGS
    ):
        return GrantLevel.FULL
    return GrantLevel.NONE


__all__ = [
    "SETTINGS_WRITE_CAPABILITY",
    "SUPPORT_READ_CAPABILITIES",
    "SupportContext",
    "current_support",
    "has_active_support_grant",
    "in_support_mode",
    "resolve_support_grant",
    "support_clients",
    "support_level",
    "support_scope",
]

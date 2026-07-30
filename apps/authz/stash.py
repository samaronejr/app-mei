"""Capability levels resolved before the portal role switch, and read after it.

`resolve_level` needs three tables — `authz_capability`, `tenants_membership` and
`authz_rolegrant` — and `app_portal` can read none of them. The membership table is the
one that cannot simply be granted: its RLS is disabled by design, so a portal SELECT on
it would be an unfiltered cross-tenant read of every firm's roster.

So the answer is computed once as `app_runtime`, while both context variables are live
and before `SET LOCAL ROLE`, and read from here afterwards. That makes this module new
mutable request state, and the rules below are its whole contract. Each exists because a
review found the shape it forbids:

* **Miss loudly.** Falling through to `resolve_level` on a portal path reads a denied
  table and raises `ProgrammingError`, which aborts the transaction and turns a refusal
  into a 500 with no usable diagnostic. Returning `NONE` instead would fail closed
  silently and read as a permissions bug.
* **Answer only for the identity it was built for.** `can()` takes any user, not just
  `request.user`.
* **Answer only for the tenant it was built for**, comparing the *effective* tenant —
  the explicit argument when given, the context variable when omitted. `require_can`
  passes no tenant, and `record_event` enters a nested tenant context, so binding only
  the explicit argument breaks the first and binding only the context variable misses
  the second.

There is deliberately no client rule. `can()` takes no client argument,
`current_client_id` is assigned once per request in `PortalMiddleware`, and its only
mutator is barred from request code by the client-id provenance guard. A comparison
would have both sides derived from one membership row and could never refuse.
"""

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from django.contrib.auth.models import AnonymousUser

from apps.accounts.models import User


class PortalStashMiss(LookupError):  # noqa: N818
    """A portal request asked something the stash was not built to answer."""


@dataclass(frozen=True, slots=True)
class PortalStash:
    """One request's resolved levels, with the identity they were resolved for."""

    user_pk: UUID
    tenant_id: UUID
    levels: Mapping[str, str]


portal_stash: Final[ContextVar[PortalStash | None]] = ContextVar(
    "portal_stash",
    default=None,
)


def stashed_level(
    user: User | AnonymousUser,
    action: str,
    tenant_id: UUID | None,
    current_tenant_id: UUID | None,
) -> str | None:
    """Return the stashed level, `None` when no stash applies, or raise on a miss.

    `None` means "no portal role was assumed, take the ordinary path" — the firm side,
    `/accounts/`, and the non-atomic path all land here. Every other disagreement is a
    miss and raises, because on a portal path the ordinary path cannot run.
    """
    stash = portal_stash.get()
    if stash is None:
        return None

    if not user.is_authenticated or user.pk != stash.user_pk:
        msg = (
            f"The portal stash was built for user {stash.user_pk!r} and was asked "
            f"about {getattr(user, 'pk', None)!r}. Answering from it would report one "
            f"account's permissions for another."
        )
        raise PortalStashMiss(msg)

    effective_tenant = tenant_id if tenant_id is not None else current_tenant_id
    if effective_tenant != stash.tenant_id:
        msg = (
            f"The portal stash was built for tenant {stash.tenant_id!r} and was asked "
            f"about {effective_tenant!r}. Answering from it would report one firm's "
            f"permissions for another."
        )
        raise PortalStashMiss(msg)

    if action not in stash.levels:
        msg = (
            f"{action!r} is outside the portal stash. Falling through would read "
            f"authz_capability as app_portal and abort the transaction."
        )
        raise PortalStashMiss(msg)

    return stash.levels[action]

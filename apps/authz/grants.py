"""Writing `UserCapabilityGrant` rows: the grant's only mutation path.

`resolve_level` READS grants; this module WRITES them, and the split is the point.
A grant is a change to what an account may do, so minting or revoking one takes the
same controls every firm-side state change in this codebase takes: a `can()`
re-check inside the function (the view's decorator is not reachable from a
management command or a future API), a row lock so two clicks or two requests
cannot interleave into a duplicated or phantom grant, an expected revision checked
under that lock, and an audit event.

The capability it administers is pinned to `actions.approve` — the S8.1
"reviewer" capability — and the seat it may land on is pinned to an active,
firm-side `staff_accountant` membership. Both pins are deliberate: a generic
grant-writing function would let a caller raise an owner (already FULL, so the row
would be dead weight that LOOKS like a grant) or, worse, hand a capability to a
client-role membership that resolve-level is required never to honour. A function
that only knows how to make reviewers cannot produce either shape.
"""

import logging
from typing import TYPE_CHECKING, Final, TypedDict
from uuid import UUID

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.models import GrantLevel, UserCapabilityGrant
from apps.authz.services import can
from apps.core.tenancy import tenant_context
from apps.tenants.authority import lock_users
from apps.tenants.models import Membership, TenantRole

if TYPE_CHECKING:
    from django_stubs_ext import WithAnnotations

logger = logging.getLogger(__name__)

# The capability a reviewer grant raises, and the capability the caller must hold
# to grant or revoke it. Both are rows in the seeded matrix; naming them here keeps
# the slugs in one place.
REVIEWER_CAPABILITY: Final[str] = "actions.approve"
ROSTER_CAPABILITY: Final[str] = "users.create"


class CapabilityGrantError(Exception):
    """Base class for every reason a capability grant cannot be written."""


class GrantNotPermittedError(CapabilityGrantError):
    """The actor holds no authority over this firm's capability grants."""


class GrantConflictError(CapabilityGrantError):
    """A roster decision was made after the caller read the grant state."""


class GrantSeatError(CapabilityGrantError):
    """The membership named cannot carry a reviewer grant.

    Raised for every seat shape the team page never offers: a role other than
    `staff_accountant` (owners and ops hold the capability FULL by role, so a grant
    would be dead weight that reads as a difference), a portal membership (a client
    role must never be raised by a user grant), and a deactivated member (granting
    to a seat nobody occupies writes a row that activates nothing but resolves
    nothing either — refuse it rather than store the ambiguity).
    """


def reviewer_eligible(membership: Membership) -> bool:
    """Report whether this membership may carry a reviewer grant.

    The rule lives in apps/authz because the role-check guard forbids a role
    comparison anywhere else — a template or view deciding "this row gets the
    toggle" from `membership.role` would be a second copy of the roster policy.
    `client__isnull` is a data-shape fact, not a role decision: it separates the
    two kinds of membership row.
    """
    return bool(
        membership.client_id is None
        and membership.role == TenantRole.STAFF_ACCOUNTANT
        and membership.is_active
    )


class ReviewerState(TypedDict):
    """Grant state annotated onto the versioned membership row."""

    reviewer_active: bool


def with_reviewer_state(
    memberships: QuerySet[Membership],
) -> "QuerySet[WithAnnotations[Membership, ReviewerState]]":
    """Read the ambient tenant's state and revision in one SQL snapshot."""
    return memberships.annotate(
        reviewer_active=Exists(
            UserCapabilityGrant.objects.filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
                user_id=OuterRef("user_id"),
                capability__slug=REVIEWER_CAPABILITY,
                revoked_at__isnull=True,
            )
        )
    )


@transaction.atomic
def set_review_capability(
    *,
    actor: User,
    membership: Membership,
    enabled: bool,
    expected_version: int,
) -> UserCapabilityGrant | None:
    """Apply a decision only to the revision the caller actually read.

    A stale retry conflicts even if its desired state currently matches. A fresh
    request for the current state is a no-op; real transitions advance the seat's
    revision atomically with the grant and audit event.
    """
    users = lock_users((actor.pk, membership.user_id))
    actor = users[actor.pk]
    # Re-read under a row lock. `revoke_invite` takes the same lock for the same
    # reason: two requests racing the same seat must serialise on something, and
    # the membership row is the seat being decided about.
    # PLATFORM_QUERY_OK: re-reads by primary key the membership the caller was
    # already scoped to with for_user in the view, purely to take that lock.
    locked = Membership.objects.select_for_update().get(pk=membership.pk)
    if (
        locked.user_id != membership.user_id
        or not users[locked.user_id].is_active
        or not reviewer_eligible(locked)
    ):
        msg = "A reviewer grant can only sit on an active staff_accountant membership."
        raise GrantSeatError(msg)
    # Re-checked against the ROW's tenant, inside its context, so a call arriving
    # without one — a management command — still answers the question about the
    # right firm rather than whatever tenant happens to be ambient.
    with tenant_context(locked.tenant_id):
        if not actor.is_active or not can(
            actor, ROSTER_CAPABILITY, tenant_id=locked.tenant_id
        ):
            msg = "This role may not administer the firm's roster."
            raise GrantNotPermittedError(msg)
        if locked.review_grant_version != expected_version:
            msg = "The reviewer grant changed; reload the team before trying again."
            raise GrantConflictError(msg)
        existing = UserCapabilityGrant.objects.filter(
            user_id=locked.user_id,
            capability__slug=REVIEWER_CAPABILITY,
            revoked_at__isnull=True,
        ).first()

        if enabled:
            if existing is not None:
                if existing.expires_at is None or existing.expires_at > timezone.now():
                    return existing
                existing.revoked_at = timezone.now()
                existing.save(update_fields=["revoked_at"])
                _record(AuditAction.CAPABILITY_REVOKED, actor=actor, grant=existing)
            locked.review_grant_version += 1
            locked.save(update_fields=["review_grant_version"])
            grant = UserCapabilityGrant.objects.create(
                tenant_id=locked.tenant_id,
                user_id=locked.user_id,
                capability_id=_reviewer_capability_pk(),
                level=GrantLevel.FULL,
                granted_by=actor,
            )
            _record(
                AuditAction.CAPABILITY_GRANTED,
                actor=actor,
                grant=grant,
            )
            logger.info(
                "capability grant issued",
                extra={
                    "user_id": str(locked.user_id),
                    "tenant_id": str(locked.tenant_id),
                },
            )
            return grant

        if existing is None:
            return None
        locked.review_grant_version += 1
        locked.save(update_fields=["review_grant_version"])
        # `update_fields` names the one column, which keeps this a MARK: a bare
        # save() would rewrite every column from the pre-lock instance.
        existing.revoked_at = timezone.now()
        existing.save(update_fields=["revoked_at"])
        _record(
            AuditAction.CAPABILITY_REVOKED,
            actor=actor,
            grant=existing,
        )
        logger.info(
            "capability grant revoked",
            extra={
                "user_id": str(locked.user_id),
                "tenant_id": str(locked.tenant_id),
            },
        )
        return existing


def _reviewer_capability_pk() -> UUID:
    """Resolve the seeded capability row this module administers."""
    from apps.authz.models import Capability  # noqa: PLC0415

    pk: UUID = Capability.objects.get(slug=REVIEWER_CAPABILITY).pk
    return pk


def _record(
    action: str,
    *,
    actor: User,
    grant: UserCapabilityGrant,
) -> None:
    """Write the tenant-attributable audit row for one grant transition.

    An `Event`, not a `PlatformEvent`: both legs of a grant's life happen with the
    firm resolved, so the row lands in the same append-only table every other
    roster action lands in. Metadata carries ids and the slug, never names or
    emails — the table is unerasable and LGPD gives the subject rights over it.
    """
    record_event(
        action=action,
        tenant_id=grant.tenant_id,
        actor=actor,
        obj=ObjectRef(type="user_capability_grant", id=str(grant.pk)),
        metadata={
            "capability": REVIEWER_CAPABILITY,
            "user_id": str(grant.user_id),
            "level": str(grant.level),
        },
    )


__all__ = [
    "REVIEWER_CAPABILITY",
    "CapabilityGrantError",
    "GrantConflictError",
    "GrantNotPermittedError",
    "GrantSeatError",
    "reviewer_eligible",
    "set_review_capability",
    "with_reviewer_state",
]

"""Issuing and redeeming invitations to join an accounting firm.

The security property this module exists to hold is that **holding the link is not
enough**. An invite arrives by email, and email is forwarded, quoted into ticket
threads, and archived in mailboxes nobody audits. If possession of the token were
sufficient, anyone downstream of that chain could join the firm and read its clients'
CPFs, CNPJs and revenue.

So redemption requires two independent facts: the token, and a session that has
**proved control of the invited mailbox**. Concretely, the accepting account must own
a *verified* email address equal to `Invite.email`. A session belonging to anyone else
is refused outright — the membership is never quietly attached to whoever happens to
be signed in.

The checks live here rather than in the view so that a future caller — a management
command, an API, a test helper — cannot reach acceptance without them.
"""

import logging
from typing import Final

from allauth.account.models import EmailAddress
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import Origin, record_platform_event
from apps.authz.services import can
from apps.tenants.models import Invite, Membership, Tenant

logger = logging.getLogger(__name__)

# The capability the RBAC matrix scores as "Create and deactivate users". Which roles
# hold it is a row in authz_rolegrant, never a constant here: T-017 shipped with an
# interim `role__in` gate precisely because can() did not exist yet, and that copy of
# the matrix would have drifted from the real one the first time a grant changed.
INVITE_CAPABILITY: Final[str] = "users.create"


class InviteError(Exception):
    """Base class for every reason an invitation cannot be issued or redeemed."""


class InviteNotFoundError(InviteError):
    """No invitation matches the presented token."""


class InviteExpiredError(InviteError):
    """The invitation's validity window has closed."""


class InviteAlreadyAcceptedError(InviteError):
    """The invitation has already been redeemed. Invites are single-use."""


class InviteEmailMismatchError(InviteError):
    """The accepting session has not proved control of the invited mailbox."""


class InviteNotPermittedError(InviteError):
    """The actor's role does not permit administering the firm's roster."""


class InviteAccountExistsError(InviteError):
    """The invited address already has an account, which must be signed in to."""


def may_issue_invites(user: User, tenant: Tenant) -> bool:
    """Report whether this account may add people to this firm.

    The tenant is passed explicitly rather than read from the ambient context: this
    function is reachable from a management command and from tests, and a check that
    silently fell back to "whatever tenant the thread happens to be in" would be
    answering a different question than the caller asked.
    """
    return can(user, INVITE_CAPABILITY, tenant_id=tenant.pk)


def issue_invite(
    *,
    actor: User,
    tenant: Tenant,
    email: str,
    role: str,
) -> tuple[Invite, str]:
    """Create an invitation, returning it with the one-time raw token to mail."""
    if not may_issue_invites(actor, tenant):
        msg = "This role may not administer the firm's roster."
        raise InviteNotPermittedError(msg)
    invite, raw_token = Invite.issue(tenant=tenant, email=email, role=role)
    # The token is deliberately absent from this record: a bearer credential in a log
    # file is a bearer credential in whatever ships those logs onward.
    logger.info(
        "invite issued",
        extra={"invite_id": str(invite.pk), "tenant_id": str(tenant.pk)},
    )
    # A PlatformEvent, not an Event: acceptance happens with no tenant resolved, so
    # the two halves of an invitation's life would otherwise land in different tables
    # and an investigation would have to join them.
    record_platform_event(
        action=AuditAction.INVITE_ISSUED,
        origin=Origin(actor=actor, subject=email),
        tenant_id=tenant.pk,
        metadata={"invite_id": str(invite.pk), "role": role},
    )
    return invite, raw_token


def resolve_invite(raw_token: str) -> Invite:
    """Look an invitation up by token and reject every unusable state."""
    # PLATFORM_QUERY_OK: the digest of a 32-byte unguessable token IS the scope
    # here, and the caller is anonymous by construction. Authorization is not skipped,
    # it is deferred to accept_invite, which binds redemption to the invited mailbox.
    invite = Invite.objects.filter(token=Invite.hash_token(raw_token)).first()
    if invite is None:
        msg = "No invitation matches that link."
        raise InviteNotFoundError(msg)
    _assert_redeemable(invite)
    return invite


def _assert_redeemable(invite: Invite) -> None:
    if invite.accepted_at is not None:
        msg = "That invitation has already been used."
        raise InviteAlreadyAcceptedError(msg)
    if invite.expires_at <= timezone.now():
        msg = "That invitation has expired."
        raise InviteExpiredError(msg)


def _controls_invited_mailbox(user: User, invite: Invite) -> bool:
    return bool(
        EmailAddress.objects.filter(
            user=user,
            email__iexact=invite.email,
            verified=True,
        ).exists(),
    )


@transaction.atomic
def accept_invite(*, invite: Invite, user: User) -> Membership:
    """Redeem an invitation for an account that has proved it owns the address."""
    # Re-read under a row lock: two clicks on the same link land in two requests, and
    # a check made outside the lock would let both create a membership.
    # PLATFORM_QUERY_OK: re-reads by primary key the invite already resolved from
    # its token, purely to take a row lock against a double redemption.
    locked = Invite.objects.select_for_update().get(pk=invite.pk)
    _assert_redeemable(locked)

    if not _controls_invited_mailbox(user, locked):
        msg = (
            "This invitation was issued to a different address. Sign in as that "
            "address, or ask the firm to re-issue it."
        )
        raise InviteEmailMismatchError(msg)

    # PLATFORM_QUERY_OK: creates the membership named by the invite, keyed to the
    # invite's own tenant. The caller cannot influence which tenant that is.
    membership, _created = Membership.objects.get_or_create(
        user=user,
        tenant_id=locked.tenant_id,
        defaults={"role": locked.role, "is_active": True},
    )
    locked.accepted_at = timezone.now()
    locked.save(update_fields=["accepted_at"])
    logger.info(
        "invite accepted",
        extra={"invite_id": str(locked.pk), "tenant_id": str(locked.tenant_id)},
    )
    record_platform_event(
        action=AuditAction.INVITE_ACCEPTED,
        origin=Origin(actor=user, subject=locked.email),
        tenant_id=locked.tenant_id,
        metadata={"invite_id": str(locked.pk), "role": locked.role},
    )
    return membership


@transaction.atomic
def register_and_accept(
    *,
    invite: Invite,
    password: str,
    full_name: str = "",
) -> tuple[User, Membership]:
    """Create the invited account and redeem the invitation in one transaction."""
    # PLATFORM_QUERY_OK: same primary-key re-read under a row lock as above.
    locked = Invite.objects.select_for_update().get(pk=invite.pk)
    _assert_redeemable(locked)

    if User.objects.filter(email__iexact=locked.email).exists():
        msg = "That address already has an account. Sign in to accept."
        raise InviteAccountExistsError(msg)

    user = User.objects.create_user(
        email=locked.email,
        password=password,
        full_name=full_name,
    )
    # Marked verified because redeeming the token IS the proof: it was delivered to
    # this mailbox and nowhere else. Leaving it unverified would make the account
    # unable to sign in under the mandatory-verification policy.
    EmailAddress.objects.create(
        user=user,
        email=locked.email,
        verified=True,
        primary=True,
    )
    return user, accept_invite(invite=locked, user=user)

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
from typing import TYPE_CHECKING, Final

from allauth.account.models import EmailAddress
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import Origin, record_platform_event
from apps.authz.services import can
from apps.tenants.models import Invite, Membership, Tenant

if TYPE_CHECKING:
    from apps.clients.models import ClientCompany

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


class InviteHostMismatchError(InviteNotFoundError):
    """The invitation belongs to a different firm than the host it was presented on.

    A subclass rather than a sibling because it IS a not-found — the answer must be that
    no invitation matches this link here. A 403 would confirm the token names a real
    invitation somewhere on the platform, which is precisely the fact a tenant boundary
    exists to withhold; `_visible_invite` answers 404 one flow over for the same reason.

    The subclassing is semantics only, NOT plumbing: `_STATUS_BY_ERROR` is keyed on
    `type(error)` exactly, so this class is listed there in its own right.
    """


class InviteScopeMismatchError(InviteNotFoundError):
    """A firm-side invitation was presented at the portal's acceptance route.

    Also a 404, and also deliberately, and also listed in `_STATUS_BY_ERROR` in its own
    right rather than relying on the parent's entry. A firm seat has its own route on
    the platform host; redeeming one here would create a membership with no client while
    the URL the invitee followed named one firm's portal, and refusing it as "not found
    here" says the true thing without reporting on the row.
    """


class InviteExpiredError(InviteError):
    """The invitation's validity window has closed."""


class InviteAlreadyAcceptedError(InviteError):
    """The invitation has already been redeemed. Invites are single-use."""


class InviteRevokedError(InviteError):
    """The firm withdrew the invitation before it could be redeemed."""


class InviteNotRevocableError(InviteError):
    """There is nothing left to withdraw: the invitation is spent or already withdrawn.

    Separate from `InviteRevokedError` because the two face opposite directions. That
    one is shown to whoever holds a dead link; this one is shown to the administrator
    who just tried to stand one down, and the sentence each needs is different.
    """


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


def _invite_metadata(invite: Invite) -> dict[str, object]:
    """Return the payload every invitation event carries, client scope included.

    `client_id` is on ALL THREE legs — issue, accept, withdraw — and it is why this is a
    function rather than three dict literals. An invitation now grants either a seat in
    the firm or a seat inside one client, and those are not the same grant: the question
    an investigation asks of a portal invitation is *whose books did this open*, and an
    answer that names only the firm cannot distinguish a MEI owner's own company from
    every other client on that firm's list.

    Written as `None` for a firm-side invitation rather than omitted, so the two kinds
    are told apart by a value present in both rather than by a key missing from one —
    a reader cannot tell an absent key from a firm invitation issued before this wave.

    A reference, never a name or a document number: `apps/audit/models.py` explains why
    an append-only table under LGPD must hold ids and nothing else.
    """
    return {
        "invite_id": str(invite.pk),
        "role": invite.role,
        "client_id": str(invite.client_id) if invite.client_id else None,
    }


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
        metadata=_invite_metadata(invite),
    )
    return invite, raw_token


def issue_client_invite(
    *,
    actor: User,
    tenant: Tenant,
    client: "ClientCompany",
    email: str,
    role: str,
) -> tuple[Invite, str]:
    """Create a PORTAL invitation into one client, with the same token lifecycle.

    A separate entry point rather than a `client=` parameter on `issue_invite`, because
    the two grants differ in what they hand over. A firm invitation opens the whole
    portfolio; this one opens exactly one company. Keeping them apart means a caller
    cannot widen a portal seat by forgetting an argument, and the read at every call
    site says which of the two is being handed out.

    The token itself is the same object in both: `Invite.issue` mints one
    `secrets.token_urlsafe(32)`, stores only its SHA-256 digest, dates it seven days out
    and returns the raw value once. Single use and the `select_for_update` redemption
    are `accept_invite`'s, shared with the firm flow rather than reimplemented here.

    `(tenant, client, role)` is fixed HERE, at issue time, and never re-read at
    redemption. The token is in the invitee's hands by then, and a seat whose scope
    could still move would be a bearer credential for an unbounded grant.

    The capability is re-checked despite the view's decorator, exactly as `issue_invite`
    does: a management command, a future API or a test helper must not reach issuance
    without it.
    """
    if not may_issue_invites(actor, tenant):
        msg = "This role may not administer the firm's roster."
        raise InviteNotPermittedError(msg)
    invite, raw_token = Invite.issue(
        tenant=tenant,
        email=email,
        role=role,
        client=client,
    )
    logger.info(
        "portal invite issued",
        extra={"invite_id": str(invite.pk), "tenant_id": str(tenant.pk)},
    )
    record_platform_event(
        action=AuditAction.INVITE_ISSUED,
        origin=Origin(actor=actor, subject=email),
        tenant_id=tenant.pk,
        metadata=_invite_metadata(invite),
    )
    return invite, raw_token


def _assert_revocable(invite: Invite) -> None:
    """Refuse to withdraw an invitation that has nothing left to withdraw.

    Both refusals are conflicts rather than permission failures: the caller holds
    `users.create`, and what is wrong is the state of this row.
    """
    if invite.accepted_at is not None:
        msg = _(
            "Este convite já foi aceito, então não há mais o que revogar. Para "
            "retirar o acesso desta pessoa, use a página da equipe.",
        )
        raise InviteNotRevocableError(msg)
    if invite.revoked_at is not None:
        msg = _("Este convite já havia sido revogado.")
        raise InviteNotRevocableError(msg)


@transaction.atomic
def revoke_invite(*, actor: User, invite: Invite) -> Invite:
    """Stand an outstanding invitation down, marking the row rather than removing it."""
    # Re-read under a row lock, for the same reason acceptance does it below: two
    # clicks on the same button arrive as two requests, and a check made outside the
    # lock lets both write a withdrawal and both record one — which turns "exactly one
    # audit row per revocation" into a tally nobody can count from.
    # PLATFORM_QUERY_OK: re-reads by primary key the invite the caller was already
    # scoped to with for_user in the view, purely to take that lock.
    locked = Invite.objects.select_for_update().get(pk=invite.pk)
    # Re-checked here and not only in the view's decorator, exactly as issue_invite
    # does: a management command, a future API or a test helper must not be able to
    # reach withdrawal without the capability that guards issuance.
    if not can(actor, INVITE_CAPABILITY, tenant_id=locked.tenant_id):
        msg = "This role may not administer the firm's roster."
        raise InviteNotPermittedError(msg)
    _assert_revocable(locked)

    # `update_fields` names one column, which is what keeps this a MARK. A bare save()
    # would rewrite every column from an instance read before the lock, and expires_at
    # is one of them.
    locked.revoked_at = timezone.now()
    locked.save(update_fields=["revoked_at"])
    logger.info(
        "invite revoked",
        extra={"invite_id": str(locked.pk), "tenant_id": str(locked.tenant_id)},
    )
    # A PlatformEvent, for the reason issue_invite gives: acceptance happens with no
    # tenant resolved, so issuance already lives here — and splitting the third leg of
    # an invitation's life into the other table would mean an investigation has to join
    # them to ask which links were live.
    record_platform_event(
        action=AuditAction.INVITE_REVOKED,
        origin=Origin(actor=actor, subject=locked.email),
        tenant_id=locked.tenant_id,
        metadata=_invite_metadata(locked),
    )
    return locked


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


def assert_portal_invite(*, invite: Invite, firm_slug: str | None) -> None:
    """Refuse an invitation that does not belong at this firm's portal door.

    Lives here rather than in the view for the reason the module docstring gives about
    every other check in this file: a management command, an API or a test helper must
    not be able to reach acceptance without it.

    Two refusals, and they close different holes.

    **The firm.** `resolve_invite` finds an invitation by token digest and nothing else
    — it has to, the caller is anonymous by construction — so a link issued by one firm
    resolves perfectly at another firm's portal host. Nothing unsafe happens next, since
    the membership is built from `invite.tenant_id` and not from the hostname; what
    happens is that the invitee is signed in to a company they reached through a door
    belonging to somebody else, and the firm whose door it was has an acceptance in its
    access log for an invitation it never issued.

    **The scope.** A FIRM-side invitation carries no client, so redeeming it here would
    create a firm-wide membership from the portal's route. That row is exactly what
    `TenantMiddleware._grant_context` accepts as proof of firm-side access, and it would
    have been minted by a flow whose whole premise is that it grants one client.
    """
    # The firm is named by its SLUG rather than its id because the slug is what the
    # hostname carries, and re-deriving an id from it would be a second lookup to answer
    # a question the unique column already answers. `invite.tenant` is a foreign key
    # into tenants_tenant, which carries no row-level policy — that is why the tenant
    # middleware can read it before any context exists, and why it is readable here with
    # both GUCs empty.
    if firm_slug is None or invite.tenant.slug != firm_slug:
        msg = "No invitation matches that link."
        raise InviteHostMismatchError(msg)
    if invite.client_id is None:
        msg = "No invitation matches that link."
        raise InviteScopeMismatchError(msg)


def _assert_redeemable(invite: Invite) -> None:
    if invite.accepted_at is not None:
        msg = "That invitation has already been used."
        raise InviteAlreadyAcceptedError(msg)
    # Checked before expiry, not after: a withdrawn invitation whose week has since
    # run out would otherwise be reported as having merely lapsed, which tells its
    # holder to ask for another one when the firm has deliberately stood them down.
    if invite.revoked_at is not None:
        msg = "That invitation was withdrawn."
        raise InviteRevokedError(msg)
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


def _firm_seat(invite: Invite, user: User) -> Membership:
    """Attach the firm-wide membership a firm-side invitation promises.

    `client=None` is a LITERAL here, never `invite.client`, and the branch above is
    written out rather than collapsed into one call passing `invite.client` for both
    kinds. Collapsed, the firm path's scope would be whatever the invitation happened to
    carry — so one bad row, or one future caller that sets `client` on a firm-role
    invite, silently becomes a firm-wide seat scoped to a client. Spelled as a literal,
    this path cannot produce anything but a firm-side row whatever the invitation says,
    and the CHECK constraint is the second line rather than the only one.

    `client` is in the LOOKUP and not in `defaults`. With the `(user, tenant, client)`
    unique constraint declared NULLS NOT DISTINCT, a lookup naming only two of the three
    columns matches this user's firm-side row AND every portal row they hold in the same
    firm, and `MultipleObjectsReturned` turns an accepted invitation into a 500.

    update_or_create, NOT get_or_create. `defaults` on get_or_create is applied on the
    CREATE branch only, so for somebody whose membership was deactivated — the (user,
    tenant, NULL) row already exists — it did nothing at all: the invitation was
    consumed, accepted_at was set, the invitee was redirected to the dashboard, and the
    row stayed is_active=False carrying the role they left with. `for_user()` filters on
    is_active, so that account resolved NOTHING in this firm and was silently locked
    out, with the one token that could have let it back in already spent. Re-hiring a
    leaver is the ordinary way in. update_or_create applies `defaults` on both branches,
    and takes its own row lock while doing it, so the re-invited role lands as well as
    the reactivation.
    """
    # PLATFORM_QUERY_OK: creates the membership named by the invite, keyed to the
    # invite's own tenant. The caller cannot influence which tenant that is.
    membership, _created = Membership.objects.update_or_create(
        user=user,
        tenant_id=invite.tenant_id,
        client=None,
        defaults={"role": invite.role, "is_active": True},
    )
    return membership


def _client_seat(invite: Invite, user: User) -> Membership:
    """Attach the seat inside ONE client that a portal invitation promises.

    `client_id=invite.client_id`, not `client=invite.client`, and that is not a
    stylistic preference. `ClientCompany` is row-level-security scoped, and this runs on
    the portal host under a path that deliberately establishes NO tenant context —
    `app.tenant_id` is the empty string, so the permissive policy matches nothing and
    the lazy fetch behind `invite.client` raises `ClientCompany.DoesNotExist` before the
    membership is ever written. The column is the same column either way; only one of
    the two spellings can be read from here.

    It is in the LOOKUP, exactly as its firm-side twin's `client=None` is, and for the
    same constraint: under NULLS NOT DISTINCT a lookup on `(user, tenant)` alone matches
    this user's firm-side row too, and `MultipleObjectsReturned` is a 500 for an invitee
    holding a perfectly good token.

    **A membership is ADDED here, never converted.** Somebody who already works at the
    firm and is then given a seat in one client ends with two rows, because the two
    grants are genuinely different and each is read by a different door:
    `TenantMiddleware` accepts only `client__isnull=True`, `PortalMiddleware` only
    `client__isnull=False`. Mutating the firm row into a client row would revoke that
    accountant's access to the firm as the side effect of handing them a portal login.

    update_or_create rather than get_or_create for the reason spelled out on the twin: a
    portal seat that was deactivated and later re-invited would otherwise consume its
    token and stay switched off.
    """
    # PLATFORM_QUERY_OK: creates the membership named by the invite, keyed to the
    # invite's own tenant and its own client. The caller influences neither.
    membership, _created = Membership.objects.update_or_create(
        user=user,
        tenant_id=invite.tenant_id,
        client_id=invite.client_id,
        defaults={"role": invite.role, "is_active": True},
    )
    return membership


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

    membership = (
        _firm_seat(locked, user)
        if locked.client_id is None
        else _client_seat(locked, user)
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
        metadata=_invite_metadata(locked),
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

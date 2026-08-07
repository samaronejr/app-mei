"""Taking an invitation back, and re-inviting somebody whose access was removed.

An invite is a bearer credential with a seven-day life, and until now only two things
ended it: redemption and the clock. Neither is a control. An invitation sent to the
wrong address — or to a candidate whose offer fell through — stays redeemable for a
week by whoever holds the link, and the link lives in a mailbox nobody audits. The
firm has no way to say "not this one, not any more".

Revocation is the third way it can end, and the shape of it is most of what this
module pins:

* the row is **marked, never deleted**. The `Invite` row IS the audit trail for a
  bearer credential — who was offered what, when, and when it was withdrawn — and a
  `DELETE` can answer none of those afterwards. It also removes the token digest, so
  a link presented later resolves to nothing and is refused as *unknown* rather than
  as *withdrawn*;
* `expires_at` is **not** repurposed as the marker. Backdating it revokes the invite
  by making it look like it timed out, which erases the difference between "the firm
  changed its mind" and "nobody got round to it" in the only record that could tell
  them apart;
* an invite belonging to another firm answers **404**, never 403. A 403 confirms the
  primary key names a real invitation somewhere on the platform, which is precisely
  the fact a tenant boundary exists to withhold. This is the one case with no
  database-layer safety net at all: `tenants_invite` carries no row-level-security
  policy (`apps/core/access.py`), and the membership-scope guard's `MEMBERSHIP_QUERY`
  deliberately does not match `Invite.objects.`
  (`tests/tenants/test_membership_scope_guard.py:46-50`), so no meta-test catches an
  unscoped lookup here. The case below is the only thing standing in for one, which
  is why it asserts the stranger's invite is still pending afterwards rather than
  merely reading a status code.

The last case is a different animal, and it is a **live defect** rather than a new
feature. `accept_invite` attaches the membership with:

    Membership.objects.get_or_create(
        user=user, tenant_id=locked.tenant_id, client=None,
        defaults={"role": locked.role, "is_active": True},
    )

Django applies `defaults` **only on create**. For somebody whose membership was
deactivated — todo 15's route — the `(user, tenant, client=NULL)` row already exists,
so `get_or_create` finds it and returns it untouched: `is_active` stays `False` and
`role` stays whatever it was before they left. The invitation is consumed, the
invitee is redirected to the dashboard, and the account is silently dead — with no
error anywhere to say so, and no second invitation able to fix it, because the token
is now spent. Re-hiring a leaver is the ordinary way into that state.

ROUTE CONTRACT. One name, reversed inside test bodies rather than at import, so a
route that does not exist yet is a failing test instead of a collection error:

    invite-revoke   POST -> withdraw a pending invitation, redirect to `team`

It takes the `Invite` primary key. POST only, matching `member-reactivate`: the act
is reversible by issuing another invitation, so a confirmation page would be a click
that protects nothing.

MODEL CONTRACT. `Invite.revoked_at` is a new nullable timestamp. Nothing else about
the row changes — `expires_at`, `accepted_at` and `token` are all read back unchanged
below, and the row is still there.

AUDIT CONTRACT. `AuditAction.INVITE_REVOKED` is a new member of the enum. Reusing
`INVITE_ISSUED` would make issuing and withdrawing the same row to every query that
asks "which invitations are outstanding?", so the helper below refuses a value equal
to either of the existing invite actions.

STATUS CONTRACT. The refusals here are deliberately asserted as "a 4xx that is
neither 403 nor 404" rather than as one number. 403 would be wrong because the caller
holds `users.create` — `apps/authz/matrix.py` scores it FULL for `owner` and
`operations_admin` — and sending an administrator to look for a missing grant is the
mistake `member_deactivate_view` documents at `apps/accounts/views.py:372-374`. 404
would be wrong because the row is in the caller's own firm and visible to them. Which
of 409 and 410 fits is the implementation's call; being refused, and unchanged, is
not.
"""

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from tests.support import sign_in
from tests.ui.factories import PASSWORD, Firm, add_member, make_firm

if TYPE_CHECKING:  # stubs-only: neither name exists at runtime in this module
    from datetime import datetime

    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

SLUG: Final = "alpha-convite"
OTHER_SLUG: Final = "beta-convite"

# Reversed inside test bodies, never here. A module-level reverse() of a route nobody
# has written raises NoReverseMatch during COLLECTION, which makes this file broken
# rather than red — and a broken file proves nothing about the missing view.
REVOKE_ROUTE: Final = "invite-revoke"

# Named as a string for the same reason: `AuditAction.INVITE_REVOKED` written at module
# level is an AttributeError at import time. `_new_action` resolves it inside a test and
# says what is missing when it cannot.
REVOKED: Final = ("INVITE_REVOKED", "invite_revoked")

# Read through `values_list(_REVOKED_COLUMN, ...)` rather than as `invite.revoked_at`.
# The attribute does not exist yet, so spelling it would be a type error in a file that
# has to stay checkable while the column it names is still being designed. Django raises
# FieldError for the same lookup at runtime, which is the failure this module wants.
_REVOKED_COLUMN: Final[str] = "revoked_at"

REFUSAL_TEMPLATE: Final = "accounts/invite_refused.html"

INVITED: Final = f"convidado@{SLUG}.example.com"
STRANGER_INVITED: Final = f"convidado@{OTHER_SLUG}.example.com"


@dataclass
class Team:
    """One firm holding every role that matters here, plus a second firm."""

    firm: Firm
    ops: User
    leaver: User
    other: Firm

    @property
    def tenant(self) -> Tenant:
        """The firm every assertion below is scoped to."""
        return self.firm.tenant


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    """Both hosts: an invite is issued on a firm's subdomain and redeemed off it."""
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def team() -> Team:
    firm = make_firm(SLUG)
    return Team(
        firm=firm,
        ops=add_member(
            firm.tenant,
            f"ops@{SLUG}.example.com",
            TenantRole.OPERATIONS_ADMIN,
        ),
        leaver=add_member(
            firm.tenant,
            f"saiu@{SLUG}.example.com",
            TenantRole.STAFF_ACCOUNTANT,
        ),
        other=make_firm(OTHER_SLUG),
    )


# ------------------------------------------------------------------------- helpers


def _revoke_url(invite: Invite) -> str:
    return reverse(REVOKE_ROUTE, args=[invite.pk])


def _accept_url(raw_token: str) -> str:
    return reverse("invite-accept", args=[raw_token])


def _issue_pending(
    tenant: Tenant,
    email: str,
    role: str = TenantRole.STAFF_ACCOUNTANT,
) -> tuple[Invite, str]:
    """Issue an invitation and prove it is genuinely outstanding before it is used.

    The gate is here rather than in a case of its own because every test below asserts
    something about withdrawing a LIVE invitation. One that was already spent, or
    already lapsed, would be refused by a rule this module is not testing — and would
    make that refusal look like the revocation working.
    """
    invite, raw_token = Invite.issue(tenant=tenant, email=email, role=role)
    assert invite.accepted_at is None, (
        "the freshly issued invite is already marked accepted, so every refusal "
        "below would be the single-use rule rather than revocation"
    )
    assert invite.expires_at > timezone.now(), (
        f"the freshly issued invite expired at {invite.expires_at}, so it is "
        f"unredeemable before anything revokes it"
    )
    return invite, raw_token


def _revoked_column() -> str:
    """Name the new column, saying what is missing when the model does not carry it.

    A gate rather than a constant. Without it, `values_list('revoked_at')` raises a
    bare `FieldError` whose message names every field on the model and none of the
    reason one is being added.
    """
    declared = {field.name for field in Invite._meta.get_fields()}
    assert _REVOKED_COLUMN in declared, (
        f"apps/tenants/models.py declares no Invite.{_REVOKED_COLUMN}. Add it as a "
        f"nullable DateTimeField — the row is the audit trail for a bearer "
        f"credential, so withdrawal has to be recorded ON it rather than by deleting "
        f"it or by backdating expires_at. Fields today: {sorted(declared)}"
    )
    return _REVOKED_COLUMN


def _revoked_at(invite: Invite) -> object:
    """Re-read the withdrawal timestamp from the database, not from a stale instance.

    Typed as `object` deliberately: the column does not exist yet, so nothing more
    specific would be honest, and every caller only ever asks whether it is None.
    """
    column = _revoked_column()
    return Invite.objects.filter(pk=invite.pk).values_list(column, flat=True).first()


def _row(invite: Invite) -> Invite:
    """Return the invitation as it now stands, refusing a row that has been deleted."""
    stored = Invite.objects.filter(pk=invite.pk).first()
    assert stored is not None, (
        f"the invitation for {invite.email!r} is gone from tenants_invite. "
        f"Revocation must MARK the row, never delete it: the row is the record that "
        f"this address was offered {invite.role!r} and that the offer was withdrawn, "
        f"and it is also what makes a later presentation of the link resolve to a "
        f"withdrawn invitation rather than to no invitation at all."
    )
    return stored


def _firm_side(user: User, tenant: Tenant) -> Membership:
    """Return the caller's firm-side row, refusing to hand back a portal identity."""
    return Membership.objects.get(user=user, tenant=tenant, client__isnull=True)


def _is_active(membership: Membership) -> bool:
    """Re-read `is_active` from the database rather than from a stale instance."""
    return Membership.objects.values_list("is_active", flat=True).get(pk=membership.pk)


def _role(membership: Membership) -> str:
    """Re-read `role` from the database rather than from a stale instance."""
    return Membership.objects.values_list("role", flat=True).get(pk=membership.pk)


def _new_action(named: tuple[str, str]) -> str:
    """Resolve the new audit action, saying what is missing when it is.

    Three gates, all here. A missing member, a member whose stored value is not the
    one this module counts rows by, and a member aliased onto one of the two invite
    actions that already exist would each make every audit assertion below hold over
    the wrong rows.
    """
    name, value = named
    member = getattr(AuditAction, name, None)
    assert member is not None, (
        f"apps/audit/models.py declares no AuditAction.{name}. The enum is "
        f"deliberately complete ahead of the features that use it — see its "
        f"docstring — so add `{name} = {value!r}, _(...)` there rather than logging "
        f"a withdrawal as something it is not."
    )
    assert str(member) == value, (
        f"AuditAction.{name} stores {str(member)!r}, expected {value!r}; the rows "
        f"counted below are selected by that stored value"
    )
    for reused in (AuditAction.INVITE_ISSUED, AuditAction.INVITE_ACCEPTED):
        assert str(member) != str(reused), (
            f"AuditAction.{name} reuses the {reused.name} value, so issuing an "
            f"invitation and withdrawing one are the same row to every query that "
            f"asks which invitations are outstanding"
        )
    return str(member)


def _audit_rows(tenant: Tenant, action: str) -> int:
    """Count audit rows for one action, across both tables the split produced.

    Counted in both because which table an action belongs in is a decision this module
    does not make: `record_event` writes tenant-scoped rows, and the invite actions
    that already exist are `PlatformEvent`s because acceptance happens with no tenant
    resolved. Summing means "exactly one row" stays a statement about the audit trail
    rather than about a table name.

    The non-vacuity gate is here, in the only way in. `Event` is row-level-security
    scoped and this connection runs as `app_runtime`, so a broken tenant context
    returns zero for every action at once — indistinguishable from an action that was
    never recorded. Every test that reaches this helper has signed somebody in through
    the real allauth flow, so a login must be on record.
    """
    logins = PlatformEvent.objects.filter(action=AuditAction.LOGIN_SUCCEEDED).count()
    assert logins, (
        "no login is on record, so the audit tables are either empty or unreadable "
        "from this connection and every count below would be zero for the wrong "
        "reason"
    )
    with tenant_context(tenant.id):
        scoped = Event.objects.filter(action=action).count()
    platform = PlatformEvent.objects.filter(
        action=action,
        tenant_id=tenant.pk,
    ).count()
    return scoped + platform


def _refused(response: "_MonkeyPatchedWSGIResponse", what: str) -> int:
    """Assert a request was declined for the state of the row, and say what it wasn't.

    Every gate is here rather than in a sentinel case, because each failure below
    would otherwise be reported as "the invitation is unchanged" while the real fault
    is that the request was accepted, or refused for an unrelated reason.
    """
    status = response.status_code
    assert HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR, (
        f"{what}: answered {status}, expected a 4xx. A redirect would mean the "
        f"request was accepted, and a 5xx would mean it crashed rather than declined."
    )
    assert status != HTTPStatus.FORBIDDEN, (
        f"{what}: answered 403, which says this account may not administer the "
        f"roster at all. It may — apps/authz/matrix.py scores users.create FULL for "
        f"owner and operations_admin. What is wrong is the state of this row, and "
        f"reporting it as a permission failure sends an administrator looking for a "
        f"grant that is already there."
    )
    assert status != HTTPStatus.NOT_FOUND, (
        f"{what}: answered 404, which says no such invitation exists in this firm. "
        f"It does, and the caller can see it on the team page; 404 is reserved for "
        f"rows outside the caller's firm."
    )
    return status


# ----------------------------------------------------------------- (a) who may revoke


def test_an_owner_revokes_a_pending_invite(team: Team) -> None:
    # Given an outstanding invitation — `users.create` is FULL for `owner` in
    # apps/authz/matrix.py
    invite, _raw_token = _issue_pending(team.tenant, INVITED)

    # When the owner withdraws it
    response = team.firm.as_owner().post(_revoke_url(invite))

    # Then it is marked withdrawn and the caller is sent back to the team page
    assert response.status_code == HTTPStatus.FOUND, response.status_code
    assert response.headers["Location"] == reverse("team")
    assert _revoked_at(invite) is not None, (
        "the request was accepted but the invitation is not marked withdrawn, so "
        "the link still redeems"
    )


def test_an_operations_admin_revokes_a_pending_invite(team: Team) -> None:
    # Given the other role the matrix grants `users.create` FULL to
    invite, _raw_token = _issue_pending(team.tenant, INVITED)

    # When the operations admin withdraws it
    response = team.firm.sign_in(team.ops).post(_revoke_url(invite))

    # Then it takes effect just as it does for the owner
    assert response.status_code == HTTPStatus.FOUND, response.status_code
    assert _revoked_at(invite) is not None


def test_a_staff_accountant_may_not_revoke_an_invite(team: Team) -> None:
    # Given a staff accountant, for whom `users.create` is NONE
    invite, _raw_token = _issue_pending(team.tenant, INVITED)

    # When they try to withdraw an invitation
    response = team.firm.as_accountant().post(_revoke_url(invite))

    # Then they are refused and the invitation is untouched. 403 here is correct and
    # is the one place it is: this account may not administer the roster at all.
    assert response.status_code == HTTPStatus.FORBIDDEN, response.status_code
    assert _revoked_at(invite) is None


# --------------------------------------------------------- (b) an invite already spent


def test_an_accepted_invite_cannot_be_revoked(team: Team) -> None:
    """Withdrawing a redeemed invitation would describe a membership it cannot undo.

    The person is in the firm by then, and `revoked_at` on a spent invitation reads
    as "this offer was withdrawn" to anyone auditing the trail later — which is false,
    and points an investigation at the wrong event entirely. Removing their access is
    `member-deactivate`'s job, and it is a different row.
    """
    # Given an invitation somebody has already redeemed
    invite, raw_token = _issue_pending(team.tenant, INVITED)
    accepted = Client().post(
        _accept_url(raw_token),
        {"full_name": "Pessoa Nova", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert accepted.status_code == HTTPStatus.FOUND, (
        f"the invitation was not redeemed ({accepted.status_code}), so the refusal "
        f"below would be about a still-pending invite and prove nothing"
    )
    spent = _row(invite)
    assert spent.accepted_at is not None, (
        "the invitation is still unaccepted, so nothing distinguishes it from the "
        "pending invites the cases above withdraw successfully"
    )

    # When the owner tries to withdraw it anyway
    response = team.firm.as_owner().post(_revoke_url(invite))

    # Then it is refused, and the record of the acceptance is exactly as it was
    _refused(response, "revoking an invitation that was already accepted")
    assert _revoked_at(invite) is None, (
        "a spent invitation is now marked withdrawn as well, so the trail claims "
        "both that this person joined and that the offer was taken back"
    )
    assert _row(invite).accepted_at == spent.accepted_at, (
        "the refusal rewrote accepted_at, which is the timestamp the membership's "
        "own provenance is read from"
    )


# ---------------------------------------------------- (c) what the withdrawn link does


def test_a_revoked_token_can_no_longer_be_redeemed(team: Team) -> None:
    # Given an invitation the owner has withdrawn. One sign-in, held in a variable:
    # every account in tests/ui/factories.py shares one TOTP secret and allauth
    # records the counter it last accepted per authenticator, so calling as_owner()
    # a second time is refused as a replay.
    invite, raw_token = _issue_pending(team.tenant, INVITED)
    withdrawn = team.firm.as_owner().post(_revoke_url(invite))
    assert withdrawn.status_code == HTTPStatus.FOUND, withdrawn.status_code
    assert _revoked_at(invite) is not None, (
        "nothing was withdrawn, so the link below is an ordinary live invitation "
        "and its refusal would be measuring some other rule"
    )
    # Anonymous, because the invitee holds the link and no session on this platform.
    anonymous = Client()

    # When the person holding the link opens it
    opened = anonymous.get(_accept_url(raw_token))

    # Then they are shown the shared refusal page rather than the acceptance form —
    # which is where the reason is announced, through the error notice partial that
    # tests/ui/test_confirmation_pages.py pins
    rendered = {template.name for template in opened.templates}
    assert REFUSAL_TEMPLATE in rendered, (
        f"the withdrawn link rendered {sorted(name for name in rendered if name)} "
        f"instead of {REFUSAL_TEMPLATE}. A revoked invitation has to be refused by "
        f"resolve_invite the way an expired or spent one is, so every caller of it "
        f"— view, management command, future API — is covered by the same check."
    )
    _refused(opened, "opening a withdrawn invitation link")

    # And posting it creates nothing at all
    submitted = anonymous.post(
        _accept_url(raw_token),
        {"full_name": "Pessoa Nova", "password1": PASSWORD, "password2": PASSWORD},
    )
    _refused(submitted, "submitting a withdrawn invitation link")
    assert not User.objects.filter(email=INVITED).exists(), (
        "an account was created from a withdrawn invitation"
    )
    assert not Membership.objects.filter(user__email=INVITED).exists()


# -------------------------------------------------------------- (d) the row that stays


def test_revocation_marks_the_row_rather_than_deleting_or_expiring_it(
    team: Team,
) -> None:
    """The invite row is the audit trail for a bearer credential, so it must survive.

    Two shortcuts are refused here, and they fail in different ways. A `DELETE` loses
    the record that this address was ever offered a seat, and makes the withdrawn link
    resolve to *no invitation* rather than to a withdrawn one. Backdating `expires_at`
    keeps the row but destroys the distinction the trail exists for: "the firm changed
    its mind" and "nobody got round to it" become the same fact.
    """
    # Given the new column, resolved before anything is withdrawn — otherwise this
    # case reports the missing route and never names what the row has to grow
    _revoked_column()

    # …and an outstanding invitation, with everything about it worth preserving
    invite, _raw_token = _issue_pending(team.tenant, INVITED)
    before = _row(invite)
    original_expiry: datetime = before.expires_at
    original_token: str = before.token

    # When the owner withdraws it
    response = team.firm.as_owner().post(_revoke_url(invite))
    assert response.status_code == HTTPStatus.FOUND, response.status_code

    # Then the row is still there, marked…
    after = _row(invite)
    assert _revoked_at(invite) is not None, (
        "the row survived but carries no revoked_at, so nothing in the database "
        "distinguishes a withdrawn invitation from a live one"
    )

    # …with its validity window untouched…
    assert after.expires_at == original_expiry, (
        f"expires_at moved from {original_expiry} to {after.expires_at}. Backdating "
        f"it revokes the invitation by making it look like it lapsed, and the one "
        f"record that could tell a withdrawal from a timeout is the one being "
        f"overwritten to do it."
    )

    # …its token digest intact, so the link resolves to THIS withdrawn invitation
    # rather than to nothing…
    assert after.token == original_token, (
        "the token digest was cleared, so presenting the link now matches no row "
        "and is refused as an unknown invitation rather than a withdrawn one"
    )

    # …and no membership conjured out of the withdrawal
    assert after.accepted_at is None
    assert not Membership.objects.filter(user__email=INVITED).exists()


# ------------------------------------------------------- (e) rows outside this firm


def test_an_invite_in_another_firm_is_not_found_rather_than_forbidden(
    team: Team,
) -> None:
    """The only guard on this lookup. `tenants_invite` has no policy and no meta-test.

    `Invite` is in `NON_TENANT_TABLES`, so the database enforces nothing, and the
    membership-scope guard's regex matches `Membership.objects.` but deliberately not
    `Invite.objects.` (tests/tenants/test_membership_scope_guard.py:46-50). An
    unscoped `Invite.objects.get(pk=...)` in the view would therefore ship green —
    except that it would withdraw another firm's invitation here, which is why the
    stranger's row is read back rather than only the status code.
    """
    # Given a live invitation belonging to an entirely different firm
    stranger, _raw_token = _issue_pending(team.other.tenant, STRANGER_INVITED)
    assert stranger.tenant_id != team.tenant.pk, (
        "the fixture put both invitations in one firm, so the request below is an "
        "ordinary in-firm revocation and the boundary goes untested"
    )

    # When this firm's owner posts its primary key
    response = team.firm.as_owner().post(_revoke_url(stranger))

    # Then it is not found — never 403, which would confirm the id names a real
    # invitation somewhere on the platform
    assert response.status_code == HTTPStatus.NOT_FOUND, (
        f"answered {response.status_code}. Scope the lookup with "
        f"`Invite.objects.for_user(request.user)` and answer 404 for everything "
        f"outside it, the way _visible_membership does at "
        f"apps/accounts/views.py:253-275."
    )

    # And, the point of the case, the other firm's invitation is still live
    assert _revoked_at(stranger) is None, (
        "this firm's owner withdrew another firm's invitation. The lookup is "
        "unscoped: nothing else — no policy, no guard test — was ever going to catch "
        "that."
    )


# ------------------------------------------- (f) re-inviting somebody who was removed


def test_re_inviting_a_deactivated_member_restores_their_access(team: Team) -> None:
    """A live defect, not a new feature: `get_or_create` applies `defaults` on create.

    `accept_invite` attaches the membership with `get_or_create(user=…, tenant_id=…,
    client=None, defaults={"role": …, "is_active": True})`. For somebody deactivated
    by `member-deactivate` the `(user, tenant, NULL)` row already exists, so the
    `defaults` are never applied: the invitation is consumed, `accepted_at` is set,
    the invitee is redirected to the dashboard — and their membership is still
    inactive, still carrying the old role. `for_user()` filters on `is_active`, so
    the account resolves nothing in this firm and the person is locked out with no
    error to show anyone. The token is spent by then, so a second invitation cannot
    repair it either.

    Re-hiring a leaver is the ordinary route into that state, which is why this case
    changes the role as well: an implementation that fixes only `is_active` leaves the
    returning colleague holding whatever they had before they left, not what they were
    just offered.
    """
    # Given a member the owner has removed, through the route that removes them
    membership = _firm_side(team.leaver, team.tenant)
    departed_as = _role(membership)
    removed = team.firm.as_owner().post(
        reverse("member-deactivate", args=[membership.pk]),
    )
    assert removed.status_code == HTTPStatus.FOUND, removed.status_code
    assert _is_active(membership) is False, (
        "the member was never deactivated, so the acceptance below would be an "
        "ordinary first join and `get_or_create` would take its create branch — the "
        "one branch that already works"
    )

    # …and a fresh invitation bringing them back in a DIFFERENT firm role
    invite, raw_token = _issue_pending(
        team.tenant,
        team.leaver.email,
        role=TenantRole.OPERATIONS_ADMIN,
    )
    assert invite.role != departed_as, (
        f"the invitation offers {invite.role!r}, the same role they left with, so "
        f"an implementation that never updates the role would pass this case"
    )

    # When they sign in and accept it. On the platform host, because that is where
    # acceptance is mounted and because TenantMiddleware refuses an authenticated
    # request on a firm's subdomain without an ACTIVE membership — which is exactly
    # the state this person is in.
    invitee = sign_in(team.leaver, PASSWORD, with_mfa=True)
    response = invitee.post(_accept_url(raw_token), {})
    assert response.status_code == HTTPStatus.FOUND, (
        f"acceptance answered {response.status_code}; nothing below is measuring "
        f"what accept_invite did to the membership"
    )
    assert _row(invite).accepted_at is not None, (
        "the invitation was not consumed, so acceptance never reached the "
        "get_or_create this case is about"
    )

    # Then they are back in the firm, in the role they were offered
    assert _is_active(membership) is True, (
        "the invitation was consumed and the membership is STILL inactive. "
        "`Membership.objects.get_or_create(..., defaults={'is_active': True})` in "
        "apps/accounts/invites.py applies `defaults` only when it creates the row, "
        "and this row already existed. The account is now silently locked out of "
        "this firm with the only token that could have let it back in already spent."
    )
    assert _role(membership) == invite.role, (
        f"the membership carries {_role(membership)!r}, the role they left with, "
        f"rather than {invite.role!r}, the role they were just invited back as — "
        f"same cause: `defaults` never runs on the update branch"
    )


# ------------------------------------------------------------------ (g) the audit row


def test_a_successful_revocation_writes_exactly_one_audit_row(team: Team) -> None:
    # Given the new action and an outstanding invitation
    revoked = _new_action(REVOKED)
    invite, _raw_token = _issue_pending(team.tenant, INVITED)
    owner = team.firm.as_owner()
    assert _audit_rows(team.tenant, revoked) == 0
    issues_before = _audit_rows(team.tenant, AuditAction.INVITE_ISSUED)

    # When the owner withdraws it
    response = owner.post(_revoke_url(invite))
    assert response.status_code == HTTPStatus.FOUND, response.status_code

    # Then exactly one row records it, under its own action
    assert _audit_rows(team.tenant, revoked) == 1, (
        "the revocation wrote no audit row, or wrote more than one — a duplicate "
        "makes the trail unusable for counting, and none makes it unusable at all"
    )
    assert _audit_rows(team.tenant, AuditAction.INVITE_ISSUED) == issues_before, (
        "the withdrawal was logged as an issuance, so no query can tell which of a "
        "firm's invitations are actually outstanding"
    )


def test_a_refused_revocation_writes_no_audit_row(team: Team) -> None:
    # Given an invitation in another firm, and this firm's owner signed in
    revoked = _new_action(REVOKED)
    stranger, _raw_token = _issue_pending(team.other.tenant, STRANGER_INVITED)
    owner = team.firm.as_owner()
    assert _audit_rows(team.other.tenant, revoked) == 0

    # When the owner posts its primary key and is refused
    response = owner.post(_revoke_url(stranger))
    assert response.status_code == HTTPStatus.NOT_FOUND, response.status_code

    # Then nothing in either firm's trail claims an invitation was withdrawn
    assert _audit_rows(team.other.tenant, revoked) == 0, (
        "a refused request wrote a revocation row, so the trail records withdrawals "
        "that never happened"
    )
    assert _audit_rows(team.tenant, revoked) == 0

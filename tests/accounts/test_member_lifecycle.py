"""Taking someone's access away, and giving it back.

`users.create` is labelled "Create and deactivate users" in the published matrix, and
until now only the first half of that sentence existed. This module specifies the
second half.

Deactivation is the one membership edit that can lock a firm out of its own account,
so most of what is below is about the refusals rather than the happy path:

* nobody may deactivate themselves, because `for_user()` filters on `is_active` and
  the request that succeeded would be the last one that account could make;
* nobody may deactivate the **last active owner**, because a firm with no owner has
  nobody left who can restore one — `users.create` is NONE for `staff_accountant`
  and the remaining `operations_admin`, if there is one, cannot promote anybody;
* a membership the caller cannot see answers **404**, never 403. A 403 tells the
  caller the row exists, which is precisely the fact a tenant boundary exists to
  withhold. Two shapes of invisible row are pinned: a portal identity inside the
  caller's own firm, and a firm-side membership in somebody else's.

The last case is the one a test suite normally misses. Checking "is there more than
one active owner?" and then writing the row is a read-then-write across a transaction
boundary, and two concurrent requests both read `2`, both pass, and both write — a
firm with two owners ends with none. That is not hypothetical arithmetic: the final
case sequences the two requests against a row lock held by the test itself, so an
implementation that counts without locking loses every owner and an implementation
that locks first keeps one.

`apps/accounts/invites.py:142-149` is the precedent for the fix, and its comment says
why in one line: "a check made outside the lock would let both create a membership."

ROUTE CONTRACT. Two names, reversed inside test bodies rather than at import, so a
route that does not exist yet is a failing test instead of a collection error:

    member-deactivate   GET  -> the confirmation page
                        POST -> perform it, redirect to `team`
    member-reactivate   POST -> perform it, redirect to `team`

Both take the `Membership` primary key.

AUDIT CONTRACT. `MEMBERSHIP_DEACTIVATED` and `MEMBERSHIP_REACTIVATED` are new members
of `AuditAction`. Reusing `ROLE_CHANGED` would make the two events indistinguishable
in the one query an investigation actually runs — "who lost access, and when" — so
the helper below refuses a value equal to it.
"""

import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import pytest
from django.db import connection, connections
from django.test import Client
from django.urls import reverse
from django.utils.html import escape
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.ui.factories import Firm, add_client, add_member, client_base, make_firm

if TYPE_CHECKING:  # stubs-only: neither name exists at runtime in this module
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

SLUG: Final = "alpha-ciclo"
OTHER_SLUG: Final = "beta-ciclo"
RACE_SLUG: Final = "gama-ciclo"

# Reversed inside test bodies, never here. A module-level reverse() of a route that
# has not been written raises NoReverseMatch during COLLECTION, which makes this file
# broken rather than red — and a broken file proves nothing about the missing view.
DEACTIVATE_ROUTE: Final = "member-deactivate"
REACTIVATE_ROUTE: Final = "member-reactivate"

# Named as strings for the same reason: `AuditAction.MEMBERSHIP_DEACTIVATED` written
# at module level is an AttributeError at import time. `_new_action` resolves them
# inside a test and says what is missing when it cannot.
DEACTIVATED: Final = ("MEMBERSHIP_DEACTIVATED", "membership_deactivated")
REACTIVATED: Final = ("MEMBERSHIP_REACTIVATED", "membership_reactivated")

# The shared error notice, as `templates/partials/pure/notice.html` emits it for
# variant='error' and as `tests/ui/test_confirmation_pages.py` already pins it. A
# refusal rendered any other way is a refusal a screen reader never interrupts for.
ERROR_NOTICE: Final = re.compile(
    r"<div\b[^>]*\bclass=[\"'][^\"']*\bnotice-error\b[^\"']*[\"'][^>]*>(.*?)</div>",
    re.DOTALL | re.IGNORECASE,
)
POST_FORM: Final = re.compile(
    r"(<form\b[^>]*\bmethod=[\"']post[\"'][^>]*>)(.*?)</form>",
    re.DOTALL | re.IGNORECASE,
)
TAGS: Final = re.compile(r"<[^>]+>")

# The consequence the confirmation page has to state. Deactivation is not a tidy-up:
# `PlatformScopedManager.for_user` filters on `is_active`, so the account stops
# resolving this firm at all. Alternatives are accepted because the wording is the
# template's business; naming the loss of access is not.
CONSEQUENCE: Final = re.compile(
    r"(perder[áa]|deixar[áa]|n[ãa]o (vai |ir[áa] |poder[áa] )?"
    r"(mais )?(poder|conseguir|ter|acessar)|sem acesso|perde o acesso)",
    re.IGNORECASE,
)

# Long enough that the two threads in the race have reached the database, short
# enough that the case stays a second or two. Correctness does not depend on either
# number: the invariant asserted at the end holds under every interleaving once the
# implementation locks. The sleeps only make the UNLOCKED implementation lose.
SETTLE: Final = 0.75
JOIN_TIMEOUT: Final = 20.0


@dataclass
class Team:
    """One firm with every kind of member this module needs, plus a second firm."""

    firm: Firm
    ops: User
    first: User
    second: User
    portal: User
    other: Firm

    @property
    def tenant(self) -> Tenant:
        """The firm every assertion below is scoped to."""
        return self.firm.tenant


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def team() -> Team:
    firm = make_firm(SLUG)
    company = add_client(
        firm,
        legal_name="Alpha Ciclo Cliente MEI",
        base=client_base(0, SLUG),
    )
    return Team(
        firm=firm,
        ops=add_member(
            firm.tenant,
            f"ops@{SLUG}.example.com",
            TenantRole.OPERATIONS_ADMIN,
        ),
        first=add_member(
            firm.tenant,
            f"alvo1@{SLUG}.example.com",
            TenantRole.STAFF_ACCOUNTANT,
        ),
        second=add_member(
            firm.tenant,
            f"alvo2@{SLUG}.example.com",
            TenantRole.STAFF_ACCOUNTANT,
        ),
        portal=add_member(
            firm.tenant,
            f"portal@{SLUG}.example.com",
            TenantRole.CLIENT_OWNER,
            client=company,
        ),
        other=make_firm(OTHER_SLUG),
    )


# ------------------------------------------------------------------------- helpers


def _firm_side(user: User, tenant: Tenant) -> Membership:
    """Return the caller's firm-side row, refusing to hand back a portal identity."""
    return Membership.objects.get(user=user, tenant=tenant, client__isnull=True)


def _portal_side(user: User, tenant: Tenant) -> Membership:
    """Return a portal identity, and prove it really is one before it is used.

    The gate is here rather than in a test of its own because every use of this row
    asserts that it is INVISIBLE to the firm-side view. A row that turned out to be
    firm-side would make that 404 correct for the wrong reason, in every case at
    once, and `pytest -k` cannot deselect a helper.
    """
    membership = Membership.objects.get(user=user, tenant=tenant, client__isnull=False)
    assert membership.client_id is not None, (
        "this row carries no client, so it is firm-side and the view is SUPPOSED to "
        "find it — the 404 asserted below would be measuring the wrong thing"
    )
    assert membership.is_active, "an inactive row would 404 for an unrelated reason"
    return membership


def _deactivate_url(membership: Membership) -> str:
    return reverse(DEACTIVATE_ROUTE, args=[membership.pk])


def _reactivate_url(membership: Membership) -> str:
    return reverse(REACTIVATE_ROUTE, args=[membership.pk])


def _is_active(membership: Membership) -> bool:
    """Re-read `is_active` from the database rather than from a stale instance."""
    return Membership.objects.values_list("is_active", flat=True).get(pk=membership.pk)


def _active_owners(tenant: Tenant) -> int:
    """Count the firm-side owners who can still sign in to `tenant`."""
    return Membership.objects.filter(
        tenant=tenant,
        client__isnull=True,
        role=TenantRole.OWNER,
        is_active=True,
    ).count()


def _new_action(named: tuple[str, str]) -> str:
    """Resolve one of the two new audit actions, saying what is missing if it is.

    Three gates, all here. A missing member, a member whose stored value is not the
    one this module counts rows by, and a member aliased onto `ROLE_CHANGED` would
    each make every audit assertion below hold over the wrong rows.
    """
    name, value = named
    member = getattr(AuditAction, name, None)
    assert member is not None, (
        f"apps/audit/models.py declares no AuditAction.{name}. The enum is "
        f"deliberately complete ahead of the features that use it — see its "
        f"docstring — so add `{name} = {value!r}, _(...)` there rather than "
        f"logging this action as something it is not."
    )
    assert str(member) == value, (
        f"AuditAction.{name} stores {str(member)!r}, expected {value!r}; the rows "
        f"counted below are selected by that stored value"
    )
    assert str(member) != str(AuditAction.ROLE_CHANGED), (
        f"AuditAction.{name} reuses the ROLE_CHANGED value, so a role edit and a "
        f"revocation of access are the same row to every audit query"
    )
    return str(member)


def _audit_rows(tenant: Tenant, action: str) -> int:
    """Count audit rows for one action, across both tables the split produced.

    Counted in both because which table an action belongs in is a decision this
    module does not make: `record_event` writes tenant-scoped rows, `require_can`'s
    refusals go to the platform table. Summing means "exactly one row" stays a
    statement about the audit trail rather than about a table name.

    The non-vacuity gate is here, in the only way in. `Event` is row-level-security
    scoped and this connection runs as `app_runtime`, so a broken tenant context
    returns zero for every action at once — indistinguishable from an action that
    was never recorded. Every test that reaches this helper has signed somebody in
    through the real allauth flow, so a login must be on record.
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


def _text(html: str) -> str:
    """Return the readable text of a fragment, with whitespace collapsed."""
    return " ".join(TAGS.sub(" ", html).split())


def _refusal_reason(response: "_MonkeyPatchedWSGIResponse", what: str) -> str:
    """Return the explanation a refused request rendered, refusing an empty one.

    Every gate is here rather than in a sentinel test, because each failure below
    would otherwise be reported as "the reasons are identical" while the real fault
    is that neither response explained anything at all.
    """
    status = response.status_code
    assert HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR, (
        f"{what}: answered {status}, expected a 4xx. A redirect would mean the "
        f"request was accepted, and a 5xx would mean it crashed rather than "
        f"declined."
    )
    body = response.content.decode()
    # Skip the layout's own furniture before looking for the page's notice.
    # `templates/base.html:153` pre-renders an INERT error notice on every page —
    # `<div id="htmx-error-fallback" class="notice notice-error mb-6" hidden>` — above
    # both `#messages` and `{% block content %}`, and deliberately gives it no role
    # (base.html:150-151: the announcement is already made by `#htmx-announce`, and a
    # second assertive element would say it twice). A plain `.search()` therefore finds
    # that one first on EVERY page and can never see the refusal underneath it.
    # `tests/ui/test_confirmation_pages.py` solves the same problem the same way — see
    # its module docstring and `_page_body` — and its `notice-error` assertion passes
    # only because it strips the layout first.
    notice = next(
        (found for found in ERROR_NOTICE.finditer(body) if "hidden" not in found[0]),
        None,
    )
    assert notice is not None, (
        f"{what}: answered {status} with no shared error notice. Render the reason "
        f"through templates/partials/pure/notice.html with variant='error', the way "
        f"accounts/invite_refused.html does — that partial is what emits "
        f'role="alert", and a refusal nobody is told about is indistinguishable '
        f"from the request having worked. Body was: {_text(body)[:400]!r}"
    )
    assert 'role="alert"' in notice.group(0), (
        f"{what}: the refusal is announced as a status rather than an alert, so a "
        f"screen reader reaches it whenever it gets round to it: {notice.group(0)}"
    )
    reason = _text(notice.group(1))
    assert reason, (
        f"{what}: the error notice is empty, so the reader is told that something "
        f"was refused and never why"
    )
    return reason


def _drain(client: Client) -> None:
    """Consume the messages allauth queued during sign-in.

    `Firm.sign_in` does not follow its redirects, so "Conectado com sucesso." is
    still pending when a test starts. Without this, a later assertion that a success
    message reached the team page would hold over the login's message and stay green
    for a view that sets none.
    """
    response = client.get(reverse("team"))
    assert response.status_code == HTTPStatus.OK, (
        f"the team page answered {response.status_code}; the sign-in did not take, "
        f"so nothing below is exercising the view it names"
    )


# ------------------------------------------------------------------- (a) who may act


def test_an_owner_deactivates_an_active_member(team: Team) -> None:
    # Given an active member of the firm, and the owner — `users.create` is FULL for
    # `owner` in apps/authz/matrix.py
    membership = _firm_side(team.first, team.tenant)
    assert _is_active(membership), "the fixture handed back an inactive member"

    # When the owner deactivates them
    response = team.firm.as_owner().post(_deactivate_url(membership))

    # Then the row is deactivated and the caller is sent back to the team page
    assert response.status_code == HTTPStatus.FOUND, response.status_code
    assert response.headers["Location"] == reverse("team")
    assert _is_active(membership) is False


def test_an_operations_admin_deactivates_an_active_member(team: Team) -> None:
    # Given the other role the matrix grants `users.create` FULL to
    membership = _firm_side(team.second, team.tenant)
    assert _is_active(membership)

    # When the operations admin deactivates a member
    response = team.firm.sign_in(team.ops).post(_deactivate_url(membership))

    # Then it takes effect just as it does for the owner
    assert response.status_code == HTTPStatus.FOUND, response.status_code
    assert _is_active(membership) is False


def test_a_staff_accountant_may_not_deactivate_anyone(team: Team) -> None:
    # Given a staff accountant, for whom `users.create` is NONE
    membership = _firm_side(team.first, team.tenant)

    # When they try to deactivate a colleague
    response = team.firm.as_accountant().post(_deactivate_url(membership))

    # Then they are refused and the row is untouched
    assert response.status_code == HTTPStatus.FORBIDDEN, response.status_code
    assert _is_active(membership) is True


# ------------------------------------------------------ (b) and (c) the two refusals


def test_deactivating_your_own_membership_is_refused_with_its_own_reason(
    team: Team,
) -> None:
    """Self-deactivation and the last-owner rule are different rules.

    Both are asserted here, in one body, so the contrast cannot be deselected. A
    single generic "não foi possível" covering both would satisfy either case on its
    own while telling the reader nothing about which rule they hit — and the two
    have completely different remedies.
    """
    # Given the operations admin, who is not an owner, so the last-owner rule cannot
    # be what refuses them
    ops_membership = _firm_side(team.ops, team.tenant)
    owner_membership = _firm_side(team.firm.owner, team.tenant)
    assert ops_membership.role != TenantRole.OWNER
    assert _active_owners(team.tenant) == 1, (
        "this firm does not have exactly one active owner, so the second half of "
        "this case would be refused by some other rule"
    )
    client = team.firm.sign_in(team.ops)

    # When they try to deactivate themselves
    mine = client.post(_deactivate_url(ops_membership))

    # Then it is refused, they keep their access, and they are told why
    reason_self = _refusal_reason(mine, "self-deactivation")
    assert _is_active(ops_membership) is True

    # And when the same account tries to remove the firm's only owner
    theirs = client.post(_deactivate_url(owner_membership))
    reason_last = _refusal_reason(theirs, "removing the last active owner")

    # Then that is a different sentence, because it is a different problem
    assert reason_self != reason_last, (
        f"both refusals render the same text, so neither says which rule applied: "
        f"{reason_self!r}"
    )


def test_deactivating_the_last_active_owner_is_refused(team: Team) -> None:
    # Given a firm with exactly one active owner. Gated rather than assumed: with two,
    # the refusal below would be the wrong rule firing and the case would pass while
    # the guarantee it names went untested.
    owner_membership = _firm_side(team.firm.owner, team.tenant)
    assert _active_owners(team.tenant) == 1, (
        f"expected exactly one active owner before the request, found "
        f"{_active_owners(team.tenant)}"
    )

    # When the operations admin tries to remove them
    response = team.firm.sign_in(team.ops).post(_deactivate_url(owner_membership))

    # Then it is refused, with a reason, and the firm still has an owner
    _refusal_reason(response, "removing the last active owner")
    assert _is_active(owner_membership) is True
    assert _active_owners(team.tenant) == 1

    # And no audit row claims otherwise
    assert _audit_rows(team.tenant, _new_action(DEACTIVATED)) == 0


# --------------------------------------------------------------------- (d) undoing it


def test_reactivation_restores_access(team: Team) -> None:
    # Given a member the owner has just deactivated
    membership = _firm_side(team.first, team.tenant)
    owner = team.firm.as_owner()
    removed = owner.post(_deactivate_url(membership))
    assert removed.status_code == HTTPStatus.FOUND, removed.status_code
    assert _is_active(membership) is False, (
        "the member was never deactivated, so the reactivation below would restore "
        "a row that was already active and prove nothing"
    )

    # When the owner reactivates them
    response = owner.post(_reactivate_url(membership))

    # Then the row is active again and the caller lands back on the team page
    assert response.status_code == HTTPStatus.FOUND, response.status_code
    assert response.headers["Location"] == reverse("team")
    assert _is_active(membership) is True


# ------------------------------------------------------- (e) and (f) what the UI says


def test_the_confirmation_page_states_the_consequence_and_carries_a_token(
    team: Team,
) -> None:
    """A confirmation that does not say what happens next is a second Yes button."""
    # Given an active member
    membership = _firm_side(team.first, team.tenant)
    url = _deactivate_url(membership)

    # When the owner opens the confirmation
    response = team.firm.as_owner().get(url)
    assert response.status_code == HTTPStatus.OK, response.status_code
    body = response.content.decode()

    # Then reading it changed nothing — a GET that acts is a link a mail scanner can
    # follow
    assert _is_active(membership) is True, (
        "the confirmation page performed the deactivation on GET"
    )

    # …it names who is about to lose access…
    text = _text(body)
    assert team.first.email in text, (
        f"the page does not name the member it is about, so the reader confirms an "
        f"unnamed row: {text[:400]!r}"
    )

    # …it says what deactivation costs them…
    assert CONSEQUENCE.search(text) is not None, (
        f"the page states no consequence. Deactivation removes the account's access "
        f"to this firm — PlatformScopedManager.for_user filters on is_active — and "
        f"a confirmation that does not say so is asking for a decision it did not "
        f"describe: {text[:400]!r}"
    )

    # …and the form that performs it is a POST carrying the token
    forms = [
        (opening, inner)
        for opening, inner in POST_FORM.findall(body)
        if url in opening or 'action=""' in opening or "action=" not in opening
    ]
    assert forms, (
        f'no <form method="post"> on the confirmation page posts back to {url}; '
        f"the page cannot confirm anything"
    )
    assert any('name="csrfmiddlewaretoken"' in inner for _opening, inner in forms), (
        "the confirming form renders no csrfmiddlewaretoken input, so it is missing "
        "{% csrf_token %} — the test client does not enforce CSRF, so this would "
        "pass every functional case here and fail for every real browser"
    )


def test_a_successful_deactivation_announces_itself_on_the_team_page(
    team: Team,
) -> None:
    # Given an active member and a session whose sign-in messages have been consumed
    membership = _firm_side(team.first, team.tenant)
    owner = team.firm.as_owner()
    _drain(owner)

    # When the owner deactivates them and follows the redirect
    response = owner.post(_deactivate_url(membership), follow=True)

    # Then it lands on the team page…
    assert response.redirect_chain, "the deactivation did not redirect anywhere"
    landed, _status = response.redirect_chain[-1]
    assert landed == reverse("team"), landed
    assert response.status_code == HTTPStatus.OK

    # …carrying a success message…
    queued = [
        (message.level_tag, str(message)) for message in response.context["messages"]
    ]
    successes = [text for tag, text in queued if tag == "success" and text.strip()]
    assert successes, (
        f"the deactivation set no success message, so the only feedback is that the "
        f"row in the table changed; messages on the response were {queued}"
    )

    # …which the layout actually rendered, rather than merely queued
    body = response.content.decode()
    assert any(escape(text) in body for text in successes), (
        f"the success message never reached the rendered page: {successes}"
    )


# ----------------------------------------------------------- (g) rows the caller owns


def test_a_portal_membership_is_not_found_rather_than_forbidden(team: Team) -> None:
    """A portal identity is not a team member, and the team page must not admit it.

    `for_user` scopes by TENANT, and a portal row carries the firm's tenant id — so
    it passes that filter. Only the `client__isnull=True` half of the pinned pattern
    at apps/accounts/views.py:214-219 excludes it.
    """
    # Given a portal identity inside the caller's own firm
    membership = _portal_side(team.portal, team.tenant)

    # When the owner posts its primary key to the firm-side route
    response = team.firm.as_owner().post(_deactivate_url(membership))

    # Then it is simply not there
    assert response.status_code == HTTPStatus.NOT_FOUND, (
        f"answered {response.status_code}. A 403 would confirm that this primary "
        f"key names a real row, which is the one fact the answer must withhold."
    )
    assert _is_active(membership) is True


def test_a_membership_in_another_firm_is_not_found_rather_than_forbidden(
    team: Team,
) -> None:
    # Given a firm-side membership belonging to an entirely different firm
    stranger = _firm_side(team.other.owner, team.other.tenant)
    assert stranger.tenant_id != team.tenant.pk

    # When this firm's owner posts its primary key
    response = team.firm.as_owner().post(_deactivate_url(stranger))

    # Then it is not found, and it is certainly not deactivated
    assert response.status_code == HTTPStatus.NOT_FOUND, (
        f"answered {response.status_code}. A 403 tells the caller a membership with "
        f"this id exists somewhere on the platform."
    )
    assert _is_active(stranger) is True
    assert _active_owners(team.other.tenant) == 1


# ------------------------------------------------------------------ (h) the audit row


def test_each_successful_action_writes_exactly_one_audit_row(team: Team) -> None:
    # Given the two new actions and a member to move between states
    deactivated = _new_action(DEACTIVATED)
    reactivated = _new_action(REACTIVATED)
    membership = _firm_side(team.first, team.tenant)
    owner = team.firm.as_owner()
    assert _audit_rows(team.tenant, deactivated) == 0
    assert _audit_rows(team.tenant, reactivated) == 0
    role_changes_before = _audit_rows(team.tenant, AuditAction.ROLE_CHANGED)

    # When the member is deactivated
    removed = owner.post(_deactivate_url(membership))
    assert removed.status_code == HTTPStatus.FOUND, removed.status_code

    # Then exactly one row records it, under its own action
    assert _audit_rows(team.tenant, deactivated) == 1, (
        "the deactivation wrote no audit row, or wrote more than one — a duplicate "
        "makes the trail unusable for counting, and none makes it unusable at all"
    )
    assert _audit_rows(team.tenant, AuditAction.ROLE_CHANGED) == role_changes_before, (
        "the deactivation was logged as a role change, so no query can tell the two "
        "apart"
    )

    # And when it is undone
    restored = owner.post(_reactivate_url(membership))
    assert restored.status_code == HTTPStatus.FOUND, restored.status_code

    # Then that is its own single row, and the first one is still there
    assert _audit_rows(team.tenant, reactivated) == 1
    assert _audit_rows(team.tenant, deactivated) == 1, (
        "the append-only trail lost or rewrote the deactivation row"
    )


# ------------------------------------------------------------------------- (i) the race


def _hold_row_lock(membership: Membership) -> "BaseDatabaseWrapper":
    """Lock one membership row from a connection of the test's own, and hold it.

    This is the sequencing device for the race. Whatever the view does to that row —
    `SELECT ... FOR UPDATE` if it locks, a bare `UPDATE` if it does not — blocks
    here, which pins the first request inside its transaction at a known point while
    the second one runs. Without it the two requests interleave by luck and an
    implementation that counts without locking passes most of the time.
    """
    holder = connections.create_connection("default")
    assert holder.settings_dict["NAME"] == connection.settings_dict["NAME"], (
        f"the holding connection points at {holder.settings_dict['NAME']!r} and the "
        f"suite at {connection.settings_dict['NAME']!r}; a lock taken in another "
        f"database blocks nobody and the race below would prove nothing"
    )
    holder.set_autocommit(False)
    table = Membership._meta.db_table
    with holder.cursor() as cursor:
        # The suppression is narrow: the only interpolation is a table name read off
        # the model meta, and the primary key travels as a bound parameter.
        cursor.execute(
            f"SELECT id FROM {table} WHERE id = %s FOR UPDATE",  # noqa: S608
            [str(membership.pk)],
        )
        assert cursor.fetchone() is not None, (
            "the row to be locked does not exist on this connection, so no lock was "
            "taken and both requests below would run unimpeded"
        )
    return holder


def _post_from_thread(
    results: dict[str, object],
    key: str,
    client: Client,
    url: str,
) -> None:
    try:
        results[key] = client.post(url).status_code
    except Exception as error:  # noqa: BLE001
        results[key] = error
    finally:
        connections.close_all()


def test_two_concurrent_deactivations_cannot_strip_the_firm_of_every_owner() -> None:
    """The read-then-write window, sequenced rather than hoped for.

    A firm with two owners, and two requests removing one each. An implementation
    that answers "are there more than one?" with an unlocked `count()` sees `2` in
    both requests, because the first has not committed when the second reads — and
    the firm ends with nobody who can restore an owner, since `users.create` is NONE
    for `staff_accountant` and there is no owner left to promote anyone.

    The sleeps only decide whether the UNLOCKED implementation loses; the assertion
    at the end holds under every interleaving once the check takes its lock first,
    which is what apps/accounts/invites.py:142-149 already does for double redemption.
    """
    # Given a firm with exactly two active owners and an administrator who may remove
    # either of them
    firm = make_firm(RACE_SLUG)
    second_owner = add_member(
        firm.tenant,
        f"owner2@{RACE_SLUG}.example.com",
        TenantRole.OWNER,
    )
    # Two administrators rather than one signing in twice: every account in this
    # suite shares one TOTP secret, and allauth records the counter it last accepted
    # per authenticator — so a second sign-in as the same person is refused as a
    # replay, and the case would die before it ever raced anything.
    ops_one = add_member(
        firm.tenant,
        f"ops1@{RACE_SLUG}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )
    ops_two = add_member(
        firm.tenant,
        f"ops2@{RACE_SLUG}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )
    first_row = _firm_side(firm.owner, firm.tenant)
    second_row = _firm_side(second_owner, firm.tenant)
    assert _active_owners(firm.tenant) == 2, (
        f"the race needs exactly two active owners to start with, found "
        f"{_active_owners(firm.tenant)} — with one, the last-owner rule refuses "
        f"both requests and this case passes without ever racing anything"
    )

    # Two signed-in sessions, established before either thread starts, so what runs
    # concurrently is the deactivation and nothing else
    one = firm.sign_in(ops_one)
    two = firm.sign_in(ops_two)
    first_url = _deactivate_url(first_row)
    second_url = _deactivate_url(second_row)

    results: dict[str, object] = {}
    threads = [
        threading.Thread(
            target=_post_from_thread,
            args=(results, "first", one, first_url),
            daemon=True,
        ),
        threading.Thread(
            target=_post_from_thread,
            args=(results, "second", two, second_url),
            daemon=True,
        ),
    ]

    # When both run, with the first pinned mid-transaction by a lock this test holds
    holder = _hold_row_lock(first_row)
    try:
        threads[0].start()
        time.sleep(SETTLE)
        threads[1].start()
        time.sleep(SETTLE)
    finally:
        holder.rollback()
        holder.close()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    alive = [index for index, thread in enumerate(threads) if thread.is_alive()]
    assert not alive, (
        f"request(s) {alive} never finished within {JOIN_TIMEOUT}s — the two "
        f"transactions deadlocked on each other rather than serialising; lock the "
        f"candidate rows in one ordered statement"
    )

    # Then the firm still has somebody who can administer it
    survivors = _active_owners(firm.tenant)
    assert survivors >= 1, (
        f"both deactivations were accepted and the firm has {survivors} active "
        f"owners left. Nobody remaining can create or restore one: `users.create` "
        f"is NONE for staff_accountant, and the check that should have refused the "
        f"second request read its count before the first committed. Take the lock "
        f"before counting. Responses were {results}"
    )
    assert len(results) == 2, f"a request produced no result at all: {results}"

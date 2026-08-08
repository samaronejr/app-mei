"""A CLIENT-scoped invitation is refused at the FIRM acceptance route.

The portal route already refuses the opposite mistake. `assert_portal_invite` turns a
firm-side invitation away from `<slug>-portal` because redeeming it there would mint
the firm-wide row `TenantMiddleware._grant_context` reads as proof of firm-side access,
out of a flow whose whole premise is that it grants one client — pinned next door in
`tests/portal/test_portal_invitations.py` under REFUSAL 3/5. This module pins the
mirror: the platform host turns a CLIENT-scoped invitation away, because redeeming it
there mints a portal seat out of a link that never named which firm's portal the seat
is in, on the one host where there is no portal context to check it against.

**The ordering is the property here, not the status code.** A guard written after
acceptance would answer 404 and still burn the token: `register_and_accept` sets
`accepted_at` under a row lock, and a single-use credential spent by the wrong door is
a credential its owner can never redeem at the right one. So the refusal cases below
end by opening the SAME token at the portal route and watching it work. That second
half is the only assertion in this file a misplaced guard cannot satisfy.

Both layers, following the five-refusal discipline next door: the scope guard is called
directly with HTTP taken out of the picture, and the route is driven through the real
middleware chain on the real host.

**The refusal sentence is deliberately identical to the one an unknown token gets.** A
wrong-scope link has to read as no link at all on this host, which is what
`_STATUS_BY_ERROR`'s own comment is about (`apps/accounts/views.py:86-89`). A distinct
sentence would answer 404 and still tell whoever holds the token that it names a real
client-scoped invitation somewhere — the fact the shared status exists to withhold. The
last case pins that indistinguishability against the two responses themselves.
"""

from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, cast

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

from apps.accounts import views as accounts_views
from apps.accounts.invites import InviteScopeMismatchError, resolve_invite
from apps.accounts.models import User
from apps.accounts.views import REFUSAL_TEMPLATE
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105
INVITED: Final = "dona@padaria.example"
FIRM_INVITED: Final = "novo@acme.example"
FIRM_SLUG: Final = "acme"
PLATFORM_HOST: Final = "testserver"
PORTAL_HOST: Final = f"{FIRM_SLUG}-portal.localhost"
FIRM_URLCONF: Final = "config.urls"
PORTAL_URLCONF: Final = "apps.portal.urls"
PADARIA_CNPJ: Final = "11222333000181"
UNKNOWN_TOKEN: Final = "nao-existe-este-token"  # noqa: S105

REGISTRATION: Final = {
    "full_name": "Dona Maria",
    "password1": PASSWORD,
    "password2": PASSWORD,
}

# The name the firm-route scope guard has to carry. Fetched off the module by name
# rather than imported at the top of this file, and that is not squeamishness: an
# ImportError at collection time collapses every case here into a single error naming a
# missing symbol, and says nothing about what the unguarded route did with the token.
# Read this way, the HTTP cases still run and still report the seat they minted.
GUARD_NAME: Final = "assert_firm_invite"

# The sentence `resolve_invite` raises for a token matching nothing at all. The
# wrong-scope refusal must be this sentence too — see the module docstring.
NOT_FOUND_SENTENCE: Final = "No invitation matches that link."

# Rendered by `accounts/invite_refused.html` and by nothing else this route can answer
# with, so it is what tells the styled refusal page apart from a bare `Http404`.
REFUSAL_SENTINEL: Final = "Pedir novo convite"


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    """Serve the SHIPPED trees, since `invite-accept` lives on only one of them.

    `config.urls` is the only tree that `include()`s `apps.accounts.urls`, so a stub
    urlconf cannot reach the route under test. The portal half needs no setting at all:
    `HostDispatchMiddleware` reassigns `request.urlconf` from the Host header and
    ignores `ROOT_URLCONF` entirely, which is why one value here serves both hosts.
    """
    settings.ROOT_URLCONF = FIRM_URLCONF
    settings.ALLOWED_HOSTS = [PLATFORM_HOST, ".localhost", "localhost"]


@pytest.fixture
def firm() -> Tenant:
    return Tenant.objects.create(name="Acme Contabilidade", slug=FIRM_SLUG)


@pytest.fixture
def padaria(firm: Tenant) -> ClientCompany:
    with tenant_context(firm.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        return ClientCompany.all_objects.create(
            tenant=firm,
            legal_name="PADARIA DA ESQUINA MEI",
            cnpj=PADARIA_CNPJ,
            is_mei=True,
        )


def _scope_guard() -> Callable[..., None]:
    """Return the firm-route scope guard, refusing a module that does not have one.

    The absence is reported as its own failure rather than as an import error, and the
    sentence says why the seam matters: a check reachable only through the view is a
    check a management command, a future API or a test helper walks straight past —
    which is the reason `apps/accounts/invites.py` gives for every guard living in it.
    """
    guard = getattr(accounts_views, GUARD_NAME, None)
    assert guard is not None, (
        f"apps/accounts/views.py exposes no {GUARD_NAME}, so the firm route cannot "
        f"refuse a client-scoped invitation below HTTP"
    )
    return cast("Callable[..., None]", guard)


def _client_invitation(firm: Tenant, padaria: ClientCompany) -> tuple[Invite, str]:
    """Mint a live PORTAL invitation, refusing a fixture that would prove nothing.

    Every gate is HERE rather than in a case, because `pytest -k` can deselect a case
    and cannot reach around a helper. In order: the invitation is genuinely
    client-scoped, so this module is not quietly exercising the firm-side flow under a
    portal-shaped name; the raw token is a non-empty string and is not what was stored;
    and — the gate this module turns on — the token is REDEEMABLE right now, before any
    wrong-route attempt. Without that last one, "still redeemable afterwards" would also
    be satisfied by a token that was never redeemable at all.

    `Invite.issue` directly rather than `issue_client_invite`: the wrapper adds the
    `users.create` gate and the audit leg, neither of which this module is about.
    """
    invite, raw_token = Invite.issue(
        tenant=firm,
        email=INVITED,
        role=TenantRole.CLIENT_OWNER,
        client=padaria,
    )
    assert invite.client_id == padaria.pk, (
        "the invitation is not client-scoped, so every case here would be exercising "
        "the firm-side flow under a portal-shaped name"
    )
    assert raw_token, "no raw token was returned, so nothing can be redeemed"
    assert invite.token != raw_token, "the raw token was stored instead of its digest"
    assert resolve_invite(raw_token).pk == invite.pk, (
        "the invitation is not redeemable before the wrong-route attempt, so proving "
        "it is still redeemable afterwards would prove nothing"
    )
    return invite, raw_token


def _firm_invitation(firm: Tenant) -> tuple[Invite, str]:
    """Mint a live FIRM-side invitation — the mirror case, which must still accept."""
    invite, raw_token = Invite.issue(
        tenant=firm,
        email=FIRM_INVITED,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    assert invite.client_id is None, (
        "the mirror fixture is client-scoped, so it cannot show the firm route still "
        "accepting what belongs to it"
    )
    assert resolve_invite(raw_token).pk == invite.pk, (
        "the mirror invitation is not redeemable, so its acceptance would prove nothing"
    )
    return invite, raw_token


def _firm_path(raw_token: str) -> str:
    return reverse("invite-accept", args=[raw_token], urlconf=FIRM_URLCONF)


def _portal_path(raw_token: str) -> str:
    return reverse("portal-invite-accept", args=[raw_token], urlconf=PORTAL_URLCONF)


def _open_at_firm_route(
    raw_token: str, method: str = "POST"
) -> "_MonkeyPatchedWSGIResponse":
    """Present the token at the firm route, anonymously, on the platform host.

    Both verbs, because they fail differently when unguarded: the GET renders the
    acceptance page and hands the invited address and the firm's name to whoever opened
    the link, and the POST goes on to mint the seat.
    """
    session = Client()
    path = _firm_path(raw_token)
    if method == "GET":
        return session.get(path, headers={"host": PLATFORM_HOST})
    return session.post(path, REGISTRATION, headers={"host": PLATFORM_HOST})


def _redeem_at_portal_route(raw_token: str) -> "_MonkeyPatchedWSGIResponse":
    return Client().post(
        _portal_path(raw_token),
        REGISTRATION,
        headers={"host": PORTAL_HOST},
        REMOTE_ADDR="203.0.113.7",
    )


def _templates(response: "_MonkeyPatchedWSGIResponse") -> list[str]:
    return [str(template.name) for template in response.templates]


def _without_the_token(response: "_MonkeyPatchedWSGIResponse", raw_token: str) -> str:
    """Return the document with the token it was asked about blanked to a placeholder.

    `templates/base.html:156` reflects `request.get_full_path` into a "Tentar novamente"
    link, so two refusals of two different tokens can never be byte-identical. That one
    difference is the INPUT, supplied by whoever opened the link and already known to
    them, so normalising it is not weakening the comparison — every other byte still has
    to match, which is where a sentence, a heading or a marker that told the two apart
    would show up.
    """
    return response.content.decode().replace(raw_token, "<the-token>")


# --------------------------------------------------------------------- the domain layer


def test_the_scope_guard_refuses_a_client_scoped_invitation_below_http(
    firm: Tenant,
    padaria: ClientCompany,
) -> None:
    # Given a live client-scoped invitation
    invite, _raw_token = _client_invitation(firm, padaria)

    # Then the guard refuses it with HTTP taken out of the picture, exactly as
    # `assert_portal_invite` refuses the mirror mistake away from the portal door
    with pytest.raises(InviteScopeMismatchError) as refused:
        _scope_guard()(invite=invite)

    # …carrying the sentence a token that names nothing at all carries, character for
    # character. `_refuse` prints `str(error)`, so this string IS the disclosure.
    assert str(refused.value) == NOT_FOUND_SENTENCE

    # …and the same call for a FIRM-side invitation does NOT raise, so the case above is
    # not passing because the guard refuses everything handed to it
    firm_invite, _firm_raw = _firm_invitation(firm)
    _scope_guard()(invite=firm_invite)


def test_the_guard_answers_not_found_rather_than_a_malformed_request() -> None:
    # Given the shared mapping both hosts answer from
    # Then this error is listed in it in its own right, at the status a link that names
    # nothing here gets. Unlisted, it would fall through to 400 and report a wrong-scope
    # link as malformed input rather than as no link at all.
    assert (
        accounts_views.status_for_invite_error(InviteScopeMismatchError(""))
        == HTTPStatus.NOT_FOUND
    )


# ----------------------------------------------------------------------- the HTTP layer


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_client_scoped_invitation_is_refused_at_the_firm_route(
    firm: Tenant,
    padaria: ClientCompany,
    method: str,
) -> None:
    # Given a live client-scoped invitation
    invite, raw_token = _client_invitation(firm, padaria)

    # When it is presented at the firm's acceptance route
    response = _open_at_firm_route(raw_token, method)
    document = response.content.decode()

    # Then it is NOT FOUND — never 403, which would confirm the token names a real
    # invitation somewhere on the platform — on the styled refusal page rather than a
    # bare Http404, and the acceptance page was never rendered at all
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert REFUSAL_TEMPLATE in _templates(response), _templates(response)
    assert REFUSAL_SENTINEL in document
    assert NOT_FOUND_SENTENCE in document
    assert accounts_views.ACCEPT_TEMPLATE not in _templates(response)

    # …and nothing at all was created. The seat this would have minted is a PORTAL seat
    # handed out on the platform host, from a link that never named which firm's portal
    # it belonged to.
    assert not User.objects.filter(email=INVITED).exists()
    assert not Membership.objects.exists()

    # …and the invited address and the firm's name were not disclosed to whoever opened
    # the link, which the acceptance page states in full
    assert INVITED not in document
    assert firm.name not in document

    # …and the token was not consumed
    assert Invite.objects.get(pk=invite.pk).accepted_at is None


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_the_refused_token_is_still_redeemable_at_the_portal_route(
    firm: Tenant,
    padaria: ClientCompany,
    method: str,
) -> None:
    """The refusal happened BEFORE the lock-and-accept, which is the whole property.

    A guard placed after `register_and_accept` answers 404 just as convincingly and
    burns the invitation on the way: `accepted_at` is set under the row lock, the
    account is created, and the person the link was mailed to is left holding a spent
    single-use credential they never got to use. Only the second half of this case can
    tell the two implementations apart.
    """
    # Given a live client-scoped invitation presented at the WRONG route and refused
    invite, raw_token = _client_invitation(firm, padaria)
    assert _open_at_firm_route(raw_token, method).status_code == HTTPStatus.NOT_FOUND

    # Then the row is untouched: unspent, unwithdrawn, still in its window
    row = Invite.objects.get(pk=invite.pk)
    assert row.accepted_at is None
    assert row.revoked_at is None

    # …and the RIGHT door still opens for it, first through the domain
    assert resolve_invite(raw_token).pk == invite.pk

    # …and then all the way through the portal route, which mints the one seat the
    # invitation always promised
    accepted = _redeem_at_portal_route(raw_token)
    assert accepted.status_code == HTTPStatus.FOUND
    seat = Membership.objects.get(user__email=INVITED)
    assert seat.client_id == padaria.pk
    assert seat.role == TenantRole.CLIENT_OWNER
    assert seat.is_active
    assert Invite.objects.get(pk=invite.pk).accepted_at is not None


def test_a_firm_side_invitation_still_accepts_at_the_firm_route(firm: Tenant) -> None:
    # Given a FIRM-side invitation, which is what this route exists for
    invite, raw_token = _firm_invitation(firm)

    # When it is redeemed at the firm route
    response = _open_at_firm_route(raw_token)

    # Then it is accepted. Without this the guard could refuse every invitation on this
    # route and every absence assertion above would still hold.
    assert response.status_code == HTTPStatus.FOUND
    seat = Membership.objects.get(user__email=FIRM_INVITED)
    assert seat.client_id is None
    assert seat.role == TenantRole.STAFF_ACCOUNTANT
    assert seat.is_active
    assert Invite.objects.get(pk=invite.pk).accepted_at is not None


def test_a_firm_side_invitation_still_renders_its_acceptance_page(firm: Tenant) -> None:
    # Given the same firm-side invitation, opened rather than submitted
    _invite, raw_token = _firm_invitation(firm)

    # When the link is followed
    response = _open_at_firm_route(raw_token, "GET")

    # Then the acceptance page is served, not the refusal. The GET half of the mirror,
    # because the guard sits ahead of both verbs and could have taken this page with it.
    assert response.status_code == HTTPStatus.OK
    assert accounts_views.ACCEPT_TEMPLATE in _templates(response)
    assert FIRM_INVITED in response.content.decode()


def test_the_refusal_is_indistinguishable_from_a_token_that_names_nothing(
    firm: Tenant,
    padaria: ClientCompany,
) -> None:
    """A wrong-scope link reads as no link at all, in the page as well as in the status.

    Sharing the 404 and then printing a different sentence would leak exactly what the
    shared status exists to withhold: that this token names a real client-scoped
    invitation. The two documents are compared whole rather than by their status,
    because the status is the half that was already right.
    """
    # Given a client-scoped invitation and a token that was never issued
    _invite, raw_token = _client_invitation(firm, padaria)

    # When both are opened at the firm route
    wrong_scope = _open_at_firm_route(raw_token, "GET")
    unknown = _open_at_firm_route(UNKNOWN_TOKEN, "GET")

    # Then the two answers are the same answer — same status, same page, and the same
    # document once the token each was asked about is taken out of it
    assert unknown.status_code == HTTPStatus.NOT_FOUND
    assert wrong_scope.status_code == unknown.status_code
    assert _templates(wrong_scope) == _templates(unknown)
    assert _without_the_token(wrong_scope, raw_token) == _without_the_token(
        unknown,
        UNKNOWN_TOKEN,
    )

    # …and the shared page really is the refusal, so this is not two identically empty
    # responses agreeing with each other
    assert REFUSAL_SENTINEL in unknown.content.decode()
    assert NOT_FOUND_SENTENCE in unknown.content.decode()

"""Every way a portal invitation dies, drawn as a page a MEI owner can read.

`tests/portal/test_portal_invitations.py` already pins what each refusal ANSWERS —
404 for a link that names nothing at this firm's door, 410 for one that existed and no
longer works, 403 for a session that is not the invitee's, 409 for an address that
already has an account — and none of that moves here. What that suite cannot see is
what the person holding the dead link is shown, because a status code is satisfied
equally by a styled page and by a plain-text body carrying an English sentence from a
domain exception. Until this wave, it was the second.

So every case below asserts the status AND the document, through one helper that
refuses both halves at once. The English strings the domain raises are asserted ABSENT
by name: they are still what `str(error)` returns, still what the firm-side refusal
page prints and still what the invitation suite matches on, and they may not be
translated where they are raised — so the only thing that can go wrong is this page
starting to print them, and that is exactly what is checked.

The structural case at the foot is the one that stops the whole flow being a 500.
`templates/accounts/` is deliberately out of scope for
`tests/ui/test_auth_templates.py`, whose scan covers `allauth`, `account` and `mfa`
because those are the trees allauth's own loader reaches — that exemption exists
because the plural directory holds firm-host screens which MAY extend the firm shell.
These two files are the first in it that may not, and nothing else guards them.
"""

import re
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import issue_client_invite, issue_invite, revoke_invite
from apps.accounts.models import User
from apps.tenants.models import Invite, TenantRole
from tests.support import enrol_totp
from tests.ui.factories import PASSWORD, Firm, add_client, make_firm

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_URLCONF: Final = "apps.portal.urls"
PROJECT_TEMPLATES: Final[Path] = Path(settings.TEMPLATES[0]["DIRS"][0])

FIRM_SLUG: Final = "acme"
OTHER_SLUG: Final = "beta"
INVITED: Final = "dona@padaria.example"

REFUSAL_TEMPLATE: Final = "accounts/portal_invite_refused.html"
ACCEPT_TEMPLATE: Final = "accounts/portal_invite_accept.html"
ENTRANCE_SHELL: Final = "allauth/layouts/entrance.html"
FIRM_SHELL: Final = "base.html"

EXTENDS_LITERAL: Final = re.compile(r"""\{%\s*extends\s+(["'])([^"']+)\1""")

STYLE_MARKER: Final = "css/app.css"
H1: Final = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
REFUSAL_HEADING: Final = "Convite indisponível"

# The interrupting role and the error ramp. Both come from the shared notice partial,
# and both are what make a refusal something a screen reader announces rather than
# something a sighted reader happens to notice is red.
ALERT: Final = 'role="alert"'
ERROR_RAMP: Final = "notice-error"

# `{% csrf_token %}` renders to this. Django's test client does not enforce CSRF, so a
# form that lost its token answers 200 in every test in this suite and 403 in
# production on the first real acceptance.
CSRF_INPUT: Final = 'name="csrfmiddlewaretoken"'

# Exactly the sentences `apps/accounts/invites.py` raises. Written out rather than
# imported, so translating one at the source — which would break the firm-side page and
# the invitation suite — reds here too instead of quietly satisfying this scan.
DOMAIN_ENGLISH: Final = (
    "No invitation matches that link.",
    "That invitation has expired.",
    "That invitation was withdrawn.",
    "That invitation has already been used.",
    "This invitation was issued to a different address",
    "That address already has an account",
)

# A distinctive fragment of each pt-BR line `_reason_for` chooses. A fragment rather
# than the whole sentence, so rewording the copy around it is not a failure while
# dropping the reason entirely is.
GONE_EXPIRED: Final = "O prazo deste convite terminou"
GONE_REVOKED: Final = "foi cancelado pelo escritório"
GONE_USED: Final = "já foi aceito"
NOT_FOUND: Final = "não corresponde a nenhum convite deste escritório"
WRONG_ADDRESS: Final = "enviado para outro endereço"
ACCOUNT_EXISTS: Final = "Já existe uma conta com este endereço"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _portal_host(slug: str) -> str:
    return f"{slug}-portal.localhost"


@pytest.fixture
def firm() -> Firm:
    """One firm with one MEI client, and an owner who may hand out portal seats."""
    made = make_firm(FIRM_SLUG)
    add_client(made, legal_name="Padaria da Esquina MEI", base="112223330001")
    return made


def _issue(firm: Firm, email: str = INVITED) -> tuple[Invite, str]:
    return issue_client_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        client=firm.clients[0],
        email=email,
        role=TenantRole.CLIENT_OWNER,
    )


def _accept_path(raw_token: str) -> str:
    return str(
        reverse("portal-invite-accept", args=[raw_token], urlconf=PORTAL_URLCONF),
    )


def _open(raw_token: str, *, slug: str = FIRM_SLUG) -> "_MonkeyPatchedWSGIResponse":
    return Client().get(
        _accept_path(raw_token),
        headers={"host": _portal_host(slug)},
    )


def _register(raw_token: str, *, slug: str = FIRM_SLUG) -> "_MonkeyPatchedWSGIResponse":
    return Client().post(
        _accept_path(raw_token),
        {"full_name": "Dona Maria", "password1": PASSWORD, "password2": PASSWORD},
        headers={"host": _portal_host(slug)},
    )


def _templates_of(response: "_MonkeyPatchedWSGIResponse") -> list[str]:
    return [template.name for template in getattr(response, "templates", [])]


def _origin_of(response: "_MonkeyPatchedWSGIResponse", name: str) -> Path:
    rendered = [
        template
        for template in getattr(response, "templates", [])
        if template.name == name
    ]
    assert rendered, (
        f"{name} did not render at all; the document was built from "
        f"{_templates_of(response)}"
    )
    origin = rendered[0].origin.name
    assert origin, f"{name} rendered from a template with no origin on disk"
    return Path(origin)


def _assert_styled_refusal(
    response: "_MonkeyPatchedWSGIResponse",
    *,
    status: HTTPStatus,
    sentence: str,
    label: str,
) -> None:
    """Refuse a dead link with a page, in pt-BR, at the status both hosts agree on.

    Every half is asserted here rather than in the cases, because each of them is
    separately satisfiable by a product that is still broken: the status alone was
    already green when this route answered `text/plain`, the stylesheet alone is green
    for any page riding our entrance shell, and the sentence alone would be green on an
    unstyled body carrying nothing else.
    """
    assert response.status_code == status, (
        f"{label}: answered {response.status_code} rather than {status}; the code is "
        f"the part that means the same thing on both hosts and it may not move"
    )
    assert REFUSAL_TEMPLATE in _templates_of(response), (
        f"{label}: the refusal was not drawn by {REFUSAL_TEMPLATE}; the document came "
        f"from {_templates_of(response)}, which for a bare body is nothing at all"
    )
    assert _origin_of(response, REFUSAL_TEMPLATE).is_relative_to(PROJECT_TEMPLATES)

    body = response.content.decode()
    assert STYLE_MARKER in body, (
        f"{label}: the refusal rendered without {STYLE_MARKER}, so no layout of ours "
        f"ran and the reader is looking at unstyled markup"
    )
    headings = [found.strip() for found in H1.findall(body)]
    assert headings == [REFUSAL_HEADING], (
        f"{label}: the page's headings are {headings}; the entrance layout owns "
        f"exactly one and this page fills it with {REFUSAL_HEADING!r}"
    )
    assert ALERT in body, (
        f"{label}: the refusal carries no interrupting announcement, so a screen "
        f"reader is never told the link failed"
    )
    assert ERROR_RAMP in body, (
        f"{label}: the refusal is drawn on some ramp other than the error one, so the "
        f"only thing marking it as a failure is the sentence inside it"
    )
    assert sentence in body, (
        f"{label}: {sentence!r} is absent, so the page says the link is unusable and "
        f"never says which of the four reasons it was"
    )
    leaked = [english for english in DOMAIN_ENGLISH if english in body]
    assert leaked == [], (
        f"{label}: the domain's own English leaked onto the page: {leaked}. Those "
        f"sentences are written for a maintainer reading a traceback; the reader here "
        f"is a MEI owner on a phone"
    )


# ------------------------------------------------------------------- 410: it is gone


def test_a_lapsed_link_says_the_deadline_passed(firm: Firm) -> None:
    # Given an invitation whose week has run out
    invite, raw_token = _issue(firm)
    invite.expires_at = timezone.now() - timedelta(seconds=1)
    invite.save(update_fields=["expires_at"])

    # Then the reader is told the prazo ended and where to get another
    _assert_styled_refusal(
        _open(raw_token),
        status=HTTPStatus.GONE,
        sentence=GONE_EXPIRED,
        label="expired",
    )


def test_a_withdrawn_link_says_the_firm_cancelled_it(firm: Firm) -> None:
    # Given an invitation the firm stood down
    invite, raw_token = _issue(firm)
    revoke_invite(actor=firm.owner, invite=invite)

    # Then it reads as CANCELLED rather than as merely lapsed. The two are the same
    # status and the same colour, and telling them apart is the whole job of the line:
    # "expired" says ask again, "cancelled" says the firm meant it.
    _assert_styled_refusal(
        _open(raw_token),
        status=HTTPStatus.GONE,
        sentence=GONE_REVOKED,
        label="revoked",
    )


def test_a_spent_link_sends_the_reader_to_sign_in_instead(firm: Firm) -> None:
    # Given an invitation that has already been redeemed once
    _invite, raw_token = _issue(firm)
    assert _register(raw_token).status_code == HTTPStatus.FOUND, (
        "the first acceptance did not succeed, so the second is not a SPENT link"
    )

    # Then the second visit says so, and points at the account that now exists rather
    # than at the firm — asking for a new invitation would be the wrong advice
    _assert_styled_refusal(
        _open(raw_token),
        status=HTTPStatus.GONE,
        sentence=GONE_USED,
        label="already accepted",
    )


# -------------------------------------------------------------- 404: not here, at all


def test_an_unknown_token_matches_nothing_without_saying_more(firm: Firm) -> None:
    # Given a link that names no invitation anywhere
    assert firm.tenant.slug == FIRM_SLUG

    # Then the page says only that, which is all a 404 is allowed to say
    _assert_styled_refusal(
        _open("nao-existe-este-token"),
        status=HTTPStatus.NOT_FOUND,
        sentence=NOT_FOUND,
        label="unknown token",
    )


def test_another_firms_door_reports_the_same_nothing(firm: Firm) -> None:
    # Given Acme's invitation and a second firm with its own portal host
    make_firm(OTHER_SLUG)
    _invite, raw_token = _issue(firm)

    # Then Beta's door says the link matches nothing HERE. Never a 403 and never a
    # different sentence: either would confirm the token names a real invitation
    # somewhere on the platform, which is the fact a tenant boundary withholds.
    _assert_styled_refusal(
        _open(raw_token, slug=OTHER_SLUG),
        status=HTTPStatus.NOT_FOUND,
        sentence=NOT_FOUND,
        label="wrong firm",
    )


def test_a_firm_side_invitation_is_nothing_at_the_portal_door(firm: Firm) -> None:
    # Given a FIRM-side invitation, which carries no client at all
    invite, raw_token = issue_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        email=INVITED,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    assert invite.client_id is None, (
        "the fixture is not firm-side, so this proves nothing"
    )

    # Then the portal refuses it as not-found rather than minting the firm-wide row
    # `TenantMiddleware` accepts as proof of firm-side access
    _assert_styled_refusal(
        _open(raw_token),
        status=HTTPStatus.NOT_FOUND,
        sentence=NOT_FOUND,
        label="firm-side invite",
    )


# ------------------------------------------------- 403 and 409: it is not yours to use


def test_the_wrong_client_s_session_is_told_which_address_the_link_wants(
    firm: Firm,
) -> None:
    # Given somebody already holding a portal seat in this firm
    _first, first_token = _issue(firm)
    _register(first_token)
    seated = User.objects.get(email=INVITED)
    enrol_totp(seated)

    # And an invitation addressed to somebody else entirely
    _other, other_token = _issue(firm, email="outro@qualquer.example")
    session = Client()
    session.force_login(seated)

    # When the seated user posts the acceptance for a link that is not theirs
    refused = session.post(
        _accept_path(other_token),
        {},
        headers={"host": _portal_host(FIRM_SLUG)},
    )

    # Then they are refused, and told the one thing that lets them act: the link
    # belongs to a different mailbox
    _assert_styled_refusal(
        refused,
        status=HTTPStatus.FORBIDDEN,
        sentence=WRONG_ADDRESS,
        label="wrong address",
    )


def test_an_address_that_already_has_an_account_is_sent_to_sign_in(
    firm: Firm,
) -> None:
    # Given an invited address that already belongs to an account
    _invite, raw_token = _issue(firm)
    existing = User.objects.create_user(email=INVITED, password=PASSWORD)
    EmailAddress.objects.create(
        user=existing,
        email=INVITED,
        verified=True,
        primary=True,
    )

    # When an anonymous visitor tries to register it a second time
    # Then the conflict is reported as one, and the way out is to sign in
    _assert_styled_refusal(
        _register(raw_token),
        status=HTTPStatus.CONFLICT,
        sentence=ACCOUNT_EXISTS,
        label="account exists",
    )


# --------------------------------------------------------------- the accepting screen


def test_the_acceptance_form_carries_a_token_it_cannot_be_posted_without(
    firm: Firm,
) -> None:
    """The one contract the test client itself cannot enforce.

    `Client()` does not check CSRF, so every acceptance in this suite and in the
    invitation suite passes with the token missing — and the first real invitee meets a
    403 on the only screen this product cannot afford to lose.
    """
    _invite, raw_token = _issue(firm)

    opened = _open(raw_token)

    assert opened.status_code == HTTPStatus.OK
    assert ACCEPT_TEMPLATE in _templates_of(opened)
    assert CSRF_INPUT in opened.content.decode(), (
        f"the acceptance form renders no {CSRF_INPUT}; Django's test client does not "
        f"enforce CSRF, so nothing else in this repository would notice"
    )


# ------------------------------------------------------------ the structural boundary


@pytest.mark.parametrize("name", [ACCEPT_TEMPLATE, REFUSAL_TEMPLATE])
def test_neither_portal_host_page_reaches_the_firm_shell(name: str) -> None:
    """The 500 that would take the whole invitation flow down, refused statically.

    `templates/base.html` renders `{% url 'dsr-submit' %}` in its footer and
    `apps/portal/urls.py` mounts that name nowhere, so a page served on the portal host
    that reaches the firm shell is a `NoReverseMatch` — not a broken link, a 500, on
    the first screen a MEI owner ever sees.

    Read off the source rather than off a render, because a render can only ever fail
    for the invitation states a fixture happens to produce, and this must hold for all
    of them. The `extends` is required to be a LITERAL for the same reason: a variable
    parent defeats every static check that could be written here.
    """
    path = PROJECT_TEMPLATES / name
    assert path.is_file(), f"{name} is not a shipped template"

    extends = EXTENDS_LITERAL.search(path.read_text("utf-8"))
    assert extends is not None, (
        f"{name} extends nothing, or extends a non-literal parent no scan can follow; "
        f"either way the shell it lands on is unknown"
    )
    assert extends.group(2) == ENTRANCE_SHELL, (
        f"{name} extends {extends.group(2)!r} rather than {ENTRANCE_SHELL!r}. The firm "
        f"shell ({FIRM_SHELL}) reverses a name the portal urlconf does not mount, so "
        f"reaching it turns every acceptance on this host into a 500"
    )

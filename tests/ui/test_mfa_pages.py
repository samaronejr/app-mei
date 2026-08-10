"""The enrolment gate, walked end to end on the portal host.

`tests/portal/test_review_regressions.py::test_an_unenrolled_portal_user_can_reach_totp_enrolment`
follows exactly one hop of this and asserts `< 500`. That was the right pin for the
defect it caught — `/accounts/` was not exempt from the portal role switch, so the
first request a new MEI owner ever makes was a 500 — but it stops one redirect short of
the page, and `< 500` is satisfied by a 302 that redirects to itself forever.

This file walks the whole chain instead, and the walk is bounded rather than followed:
`follow=True` reports neither how many hops it took nor that it took a finite number,
so a gate that redirects to a page the gate itself guards would hang the suite rather
than fail it. `_walk` counts every 3xx and refuses the fifth.

The chain is longer than it looks, and the extra hop is not an artefact of the test.
allauth decorates `ActivateTOTPView.dispatch` with `reauthentication_required`
unconditionally, so a session that cannot show a recent authentication is diverted to
`account_reauthenticate` first. `force_login` writes the session key directly and
records no authentication method, so it always is — the same as a real owner returning
after `ACCOUNT_REAUTHENTICATION_TIMEOUT`. The walk answers that challenge with the
password, which is what a person does, and which keeps this file off the genuine
allauth+TOTP sign-in in `tests/ui/factories.py` and its three known flakes. No
`totp_code()` is computed here.

WHAT IS RED TODAY, AND WHY THE OBVIOUS MARKER IS NOT.

`css/app.css` is already on the activation page. `mfa/totp/activate_form.html` extends
`mfa/base_manage.html`, which extends `allauth/layouts/manage.html`, which extends
`allauth/layouts/base.html` — and the loader resolves that last name to THIS project's
file, the one `tests/ui/test_auth_templates.py` owns. So the stylesheet, the card, the
skip link and the single `h1` all arrive without a byte of ours under `mfa/`. Asserting
the stylesheet alone would be green on arrival and could never go red, which is the
same vacuity todo 19 avoided by pairing every render with an origin check.

It is asserted anyway, as the regression half: it is what proves call 2 did not break
the inherited chain on its way to composing the page. The half that must go red is the
composition — the template that rendered the page must be OURS, and it must write its
own `head_title` rather than inherit allauth's compiled pt-BR string. Both are read off
the response that was actually rendered, not off the loader, so a file that exists but
never reaches the document cannot satisfy them.

THE INVITATION ENTRANCE, ADDED AT THE FOOT OF THIS FILE.

The three legs above enter the gate as an account that already exists. A real MEI owner
never does: they arrive holding a link their accountant sent, with no account, no seat
and no session, and the enrolment gate is something they meet on the way rather than
somewhere they start. `test_a_client_invited_from_the_firm_reaches_inicio_on_day_one`
walks that whole road — the firm's own screen, the invitation, the acceptance form, the
gate these legs already pin, and the landing page on the far side of it — reusing
`_walk` rather than restating it, so the bound, the never-leave-the-host rule and the
reauthentication answer are the same ones the legs are measured against.

It computes one real TOTP code, which the paragraph above says this file does not.
That is the single unavoidable exception: enrolment is the step under test there, and
there is no way to complete it without answering the authenticator. `tests/support.py`
documents the time-step boundary this can land on.
"""

import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import pytest
from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import pinned_totp_window, totp_code
from tests.ui.factories import Firm, add_client, assign, make_firm

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST: Final = "acme-portal.localhost"
PORTAL_URLCONF: Final = "apps.portal.urls"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105

PROJECT_TEMPLATES: Final[Path] = Path(settings.TEMPLATES[0]["DIRS"][0])

# The compiled stylesheet, same marker `tests/ui/test_account_pages.py` uses.
STYLE_MARKER: Final = "css/app.css"

# The top-level heading. The layout owns exactly one; the page's own title goes through
# the `h1` element, which this project renders as a paragraph.
H1: Final = re.compile(r"<h1[\s>]", re.IGNORECASE)

REDIRECTED: Final = range(300, 400)

# The bound. Measured on the three walks below: 3 redirects from `/`, 4 from the login
# form, 0 from logout. Five is therefore one hop of slack over the longest real chain,
# which is the point — a gate that diverts to a page it guards fails here on the fifth
# hop instead of spinning until the runner is killed.
MAX_REDIRECTS: Final = 5


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def portal_user() -> User:
    """A client-role user with NO second factor — the state a new MEI owner is in."""
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ACME",
            cnpj="11222333000181",
            is_mei=True,
        )
    user = User.objects.create_user(email="novo@mei.example", password=PASSWORD)
    Membership.objects.create(
        user=user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    return user


def _path(name: str) -> str:
    return reverse(name, urlconf=PORTAL_URLCONF)


@dataclass(frozen=True)
class Leg:
    """One walkthrough: where a person enters, and the document they must end on."""

    label: str
    entry: str
    terminal: str
    # The page-level template that must render the terminal document, and the title it
    # must write. Both are read off the rendered response.
    page_template: str
    title: str
    # The gate must divert at least this many times. Zero only where the path is
    # exempt by design; anything else would let a middleware that stopped enforcing
    # pass this file unnoticed.
    least_redirects: int


def _legs() -> tuple[Leg, ...]:
    activate = _path("mfa_activate_totp")
    return (
        Leg(
            label="root",
            entry="/",
            terminal=activate,
            page_template="mfa/totp/activate_form.html",
            title="Ativar autenticador",
            least_redirects=1,
        ),
        Leg(
            label="login",
            entry=_path("account_login"),
            terminal=activate,
            page_template="mfa/totp/activate_form.html",
            title="Ativar autenticador",
            least_redirects=1,
        ),
        Leg(
            label="logout",
            entry=_path("account_logout"),
            terminal=_path("account_logout"),
            page_template="account/logout.html",
            title="Sair",
            # `/accounts/` is exempt: `apps/accounts/mfa.py` says a half-enrolled user
            # must always be able to reach logout, so this one is served directly.
            least_redirects=0,
        ),
    )


def _walk(http: Client, leg: Leg) -> "_MonkeyPatchedWSGIResponse":
    """Follow `leg` by hand and return the document it ends on.

    Every gate the walk depends on lives here rather than in a separate test, so a
    change that makes the walk trivial — the middleware ceasing to divert, the chain
    never terminating, the reauthentication challenge silently re-rendering — fails the
    legs themselves instead of leaving them quietly asserting nothing.
    """
    reauthenticate = _path("account_reauthenticate")
    url = leg.entry
    trail: list[str] = []
    redirects = 0

    while True:
        trail.append(url)
        response = http.get(url, headers={"host": PORTAL_HOST})

        if response.status_code in REDIRECTED:
            redirects += 1
            # The bound, enforced where the loop is rather than after it. `follow=True`
            # would spin here forever and take the whole suite with it.
            assert redirects < MAX_REDIRECTS, (
                f"{leg.label}: still redirecting after {redirects} hops, so this is a "
                f"loop and not a chain: {trail}"
            )
            location = response.headers["Location"]
            # An absolute Location is allowed to exist; leaving the portal host is not.
            elsewhere = urlsplit(location).netloc
            assert elsewhere in {"", PORTAL_HOST}, (
                f"{leg.label}: the gate redirected to {location}, which leaves "
                f"{PORTAL_HOST}"
            )
            url = location
            continue

        if response.wsgi_request.path == reauthenticate:
            # allauth guards TOTP activation behind a recent authentication, so the
            # walk answers with the password. If this challenge ever stops being a
            # challenge — a re-rendered form rather than a redirect — the walk would
            # otherwise settle here and every assertion below would be about the wrong
            # page.
            answered = http.post(
                response.wsgi_request.get_full_path(),
                {"password": PASSWORD},
                headers={"host": PORTAL_HOST},
            )
            assert answered.status_code in REDIRECTED, (
                f"{leg.label}: the reauthentication challenge answered "
                f"{answered.status_code} instead of resuming the suspended request, so "
                f"the password was refused and the walk never leaves this page"
            )
            redirects += 1
            assert redirects < MAX_REDIRECTS, (
                f"{leg.label}: {redirects} hops and still not on a document: {trail}"
            )
            url = answered.headers["Location"]
            continue

        break

    # The walk went somewhere. A leg whose entry is already its terminal and whose
    # gate stopped diverting would otherwise assert only that one page renders.
    assert redirects >= leg.least_redirects, (
        f"{leg.label}: the gate diverted {redirects} times, fewer than the "
        f"{leg.least_redirects} this path requires — MFAEnforcementMiddleware is no "
        f"longer enforcing, and the rest of this leg is about whatever was served"
    )
    assert response.status_code == HTTPStatus.OK, (
        f"{leg.label}: the walk ended on {response.status_code} at {url}: {trail}"
    )
    assert response.wsgi_request.path == leg.terminal, (
        f"{leg.label}: the walk ended at {response.wsgi_request.path}, not "
        f"{leg.terminal}: {trail}"
    )
    # Still the portal, both by what the browser sent and by which tree served it.
    assert response.wsgi_request.get_host() == PORTAL_HOST, (
        f"{leg.label}: ended on host {response.wsgi_request.get_host()}"
    )
    assert getattr(response.wsgi_request, "urlconf", None) == PORTAL_URLCONF, (
        f"{leg.label}: ended on urlconf "
        f"{getattr(response.wsgi_request, 'urlconf', None)}, so the firm's tree served "
        f"a page reached through the portal host"
    )
    body = response.content.decode()
    assert body.rstrip().endswith("</html>"), (
        f"{leg.label}: the terminal answered 200 with no closing document tag, so a "
        f"block was dropped and the page is a fragment: {body[:200]!r}"
    )
    return response


def _origin_of(
    response: "_MonkeyPatchedWSGIResponse",
    name: str,
    label: str,
) -> Path:
    """Return the file the RENDERED document's page-level template came from.

    Read off `response.templates` rather than asked of the loader. A file that exists
    under `templates/` but is never reached — shadowed, or extended by nothing the view
    renders — satisfies a loader check and contributes nothing to the page.
    """
    rendered = [template for template in response.templates if template.name == name]
    assert rendered, (
        f"{label}: {name} did not render at all; the document was built from "
        f"{[template.name for template in response.templates]}"
    )
    origin = rendered[0].origin.name
    assert origin, f"{label}: {name} rendered from a template with no origin on disk"
    return Path(origin)


@pytest.mark.parametrize("leg", _legs(), ids=lambda leg: leg.label)
def test_the_enrolment_walkthrough_ends_on_a_page_this_project_composed(
    portal_user: User,
    leg: Leg,
) -> None:
    # Given a portal user with no second factor, signed in the way the existing pin
    # signs one in — `force_login`, which sidesteps the real TOTP path entirely
    http = Client()
    http.force_login(portal_user)

    # When the chain is followed to whatever document it ends on
    response = _walk(http, leg)
    body = response.content.decode()

    # Then one of this project's layouts drew it. Green before `templates/mfa/**`
    # existed, and deliberately so: `allauth/layouts/manage.html` extends
    # `allauth/layouts/base.html`, which the loader resolves to ours. This is the
    # regression half — it fails only if composing the page cost the inherited shell.
    assert STYLE_MARKER in body, (
        f"{leg.label}: {leg.page_template} rendered without {STYLE_MARKER}, so no "
        f"layout of ours ran and the enrolment page a new MEI owner lands on is bare "
        f"upstream markup"
    )

    # And it carries the layout's single heading, not a second one of its own.
    assert len(H1.findall(body)) == 1, (
        f"{leg.label}: {leg.page_template} rendered {len(H1.findall(body))} top-level "
        f"headings; the layout owns exactly one and the page title is a paragraph"
    )

    # And the page itself is this project's file. Status, stylesheet and heading count
    # are all satisfied by allauth's own template riding our inherited layout, so this
    # is the assertion that separates a composed page from an inherited one.
    origin = _origin_of(response, leg.page_template, leg.label)
    assert origin.is_relative_to(PROJECT_TEMPLATES), (
        f"{leg.label}: {leg.page_template} rendered from {origin}, which is not under "
        f"{PROJECT_TEMPLATES} — the page is still upstream's"
    )

    # And it wrote its own title. An override that extends its parent and composes
    # nothing would pass the origin check above and still inherit allauth's compiled
    # pt-BR string, so the two are asserted together.
    assert f"<title>{leg.title}</title>" in body, (
        f"{leg.label}: {leg.page_template} did not set head_title to {leg.title!r}; "
        f"the document still carries whichever title it inherited"
    )


# ------------------------------------------ the invitation entrance to the same gate
#
# Everything above enters as an account that already exists. This is the road a MEI
# owner actually travels, and it crosses four surfaces that fail independently:
#
#   1. the firm's client screen, where an accountant decides this client should be
#      able to watch their own obligations, and where the control has to be VISIBLE to
#      a role that holds `users.create` and absent from one that does not;
#   2. the mailed link, which has to name that firm's portal host and no other;
#   3. the acceptance page on the portal host, which cannot be the firm-side accept
#      template — that one reaches the firm shell, whose footer reverses a name the
#      portal urlconf does not mount, so it is a NoReverseMatch 500 here;
#   4. the enrolment gate the legs above pin, and the landing page past it.
#
# Walked as ONE test rather than four, because the interesting failures are the joins.
# Each of the four is separately green in a product where the invited owner still
# cannot reach Início.

# DERIVED from PORTAL_HOST, never spelled beside it. `_walk` sends every request to that
# constant, so the firm this walk invites from has to be the firm that host names — and
# the failure when the two drift is silent: an unresolvable slug leaves `request.client`
# unset, the landing page answers 200 with its no-company branch, and every status,
# origin and redirect assertion still holds.
INVITE_FIRM_SLUG: Final = PORTAL_HOST.removesuffix(".localhost").removesuffix("-portal")
INVITE_FIRM_HOST: Final = f"{INVITE_FIRM_SLUG}.localhost"
INVITED_EMAIL: Final = "dona@padaria.example"
INVITED_CLIENT: Final = "Padaria da Esquina MEI"

# The acceptance path out of the mailed body. Matched rather than rebuilt from the
# token, because what this walk is testing is that the link a person receives works —
# a path reconstructed here would be green against a message nobody could act on.
ACCEPT_PATH: Final = re.compile(r"(/convites/aceitar/[^/\s]+/)")

# The shared secret, read off the enrolment page's readonly control. That control
# exists so somebody without a camera can enrol; here it is the only way the walk can
# answer the authenticator, which makes this assertion a live pin on it.
SECRET_FIELD: Final = re.compile(r'id="authenticator_secret"[^>]*\svalue="([^"]+)"')

ACCEPT_TEMPLATE: Final = "accounts/portal_invite_accept.html"
DETAIL_TEMPLATE: Final = "clients/detail.html"
PORTAL_HOME_TEMPLATE: Final = "portal/home.html"

# The portal's own template tree, which is an app directory rather than the project one
# every other origin check in this file uses.
PORTAL_TEMPLATE_ROOT: Final[Path] = (
    Path(settings.BASE_DIR) / "apps" / "portal" / "templates"
)

# The layout's single heading, read for what is INSIDE it. The class list is not pinned:
# what this walk cares about is that the invitee's own company names the page, and
# retuning the type scale is not a defect.
HEADING = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)

CURRENT_PAGE: Final = 'aria-current="page"'

# The label on the firm-side control, and the label on the portal-side submit. Both are
# the msgid itself: `locale/` ships no compiled catalogue.
INVITE_ACTION_LABEL: Final = "Convidar para o portal"
ACCEPT_LABEL: Final = "Aceitar convite"

# What the portal's bar calls the landing page, and what the layout titles it.
INICIO: Final = "Início"
PORTAL_TITLE: Final = "Portal do cliente"


@pytest.fixture
def invited_firm() -> Firm:
    """A firm with one MEI client, and a staff accountant assigned to it.

    The accountant is not decoration. `users.create` is NONE for that role, so they are
    the control proving the invitation form is gated rather than merely present — and
    the assignment is what lets them reach the client's page at all, so an absent form
    cannot be passing because the whole screen was refused.
    """
    firm = make_firm(INVITE_FIRM_SLUG)
    company = add_client(firm, legal_name=INVITED_CLIENT, base="112223330001")
    assign(firm, company, firm.accountant)
    return firm


def _firm_session(user: User) -> Client:
    """Sign in on the firm host without computing a code.

    `force_login` rather than the real allauth flow, for the reason the module docstring
    gives: the firm side is the SETTING of this walk, not its subject, and driving a
    genuine TOTP sign-in here would add a second time-step boundary to a test that
    already has to cross one.
    """
    http = Client()
    http.force_login(user)
    return http


def _detail_body(http: Client, firm: Firm) -> str:
    target = reverse("client-detail", args=[firm.clients[0].pk])
    response = http.get(target, headers={"host": INVITE_FIRM_HOST})
    assert response.status_code == HTTPStatus.OK, (
        f"the client screen answered {response.status_code} at {target}, so nothing "
        f"read off it is a statement about a rendered page"
    )
    assert _origin_of(response, DETAIL_TEMPLATE, "client detail").is_relative_to(
        PROJECT_TEMPLATES,
    )
    return str(response.content.decode())


def _accept_path_from_outbox() -> str:
    assert mail.outbox, (
        "no message was sent, so the invitee never received a link and every hop "
        "below would be walking a path this test invented"
    )
    body = str(mail.outbox[-1].body)
    assert f"{INVITE_FIRM_SLUG}-portal." in body, (
        f"the mailed link does not name {INVITE_FIRM_SLUG}'s portal host: {body!r}. A "
        f"link built against the firm subdomain or the platform host lands the invitee "
        f"where the acceptance route is not mounted at all"
    )
    found = ACCEPT_PATH.search(body)
    assert found is not None, f"no acceptance path in the message body: {body!r}"
    return found.group(1)


def _secret_from(body: str) -> str:
    found = SECRET_FIELD.search(body)
    assert found is not None, (
        "the enrolment page renders no readable authenticator secret, so somebody "
        "without a camera cannot enrol and this walk cannot answer the code"
    )
    return found.group(1)


def test_the_invitation_control_is_offered_only_to_a_role_that_may_use_it(
    invited_firm: Firm,
) -> None:
    """Visibility is convenience; the decorator is the authorization.

    Both halves are asserted, because either alone is the wrong lesson. A hidden form
    that is not actually guarded is a control anybody can post to; a guarded form drawn
    for everybody is a button that teaches an accountant the product is broken.
    """
    company = invited_firm.clients[0]
    issue = reverse("portal-invite-issue", args=[company.pk])

    # Given the owner, who holds `users.create`
    offered = _detail_body(_firm_session(invited_firm.owner), invited_firm)
    assert f'action="{issue}"' in offered, (
        f"the owner's client screen draws no form posting to {issue}; the one control "
        f"§17-G authorises on this page is missing"
    )
    assert INVITE_ACTION_LABEL in offered, (
        f"{INVITE_ACTION_LABEL!r} is absent from the owner's client screen, so the "
        f"control is unlabelled or drawn under some other name"
    )

    # And the staff accountant, who does not — reaching the same client through their
    # own assignment, so the absence below is the gate and not a refused page
    withheld = _detail_body(_firm_session(invited_firm.accountant), invited_firm)
    assert company.legal_name in withheld, (
        "the accountant did not actually reach the client's page, so the absences "
        "below hold for the wrong reason"
    )
    assert f'action="{issue}"' not in withheld, (
        "the accountant is offered the invitation form even though `users.create` is "
        "NONE for their role; the endpoint answers 403, so this is a dead control"
    )
    assert INVITE_ACTION_LABEL not in withheld

    # Then posting anyway is still refused, which is the half the template cannot do
    refused = _firm_session(invited_firm.accountant).post(
        issue,
        {"email": INVITED_EMAIL, "role": TenantRole.CLIENT_OWNER},
        headers={"host": INVITE_FIRM_HOST},
    )
    assert refused.status_code == HTTPStatus.FORBIDDEN, (
        f"a role without `users.create` posted the invitation form and was answered "
        f"{refused.status_code}; hiding the control is not what refuses it"
    )


def test_a_client_invited_from_the_firm_reaches_inicio_on_day_one(
    invited_firm: Firm,
) -> None:
    """The whole road: the firm's screen to Início, on the hosts that actually serve it.

    The acceptance criterion for the invitation UI, and it is deliberately end-to-end.
    Every hop below has its own narrower pin somewhere in this suite; what none of them
    can see is whether the road joins up — and a MEI owner who cannot reach their own
    landing page does not care which of the five surfaces let go.
    """
    company = invited_firm.clients[0]

    # Given an accountant on the client's own screen, who invites them to the portal
    # through the control that screen now draws
    issue = reverse("portal-invite-issue", args=[company.pk])
    assert f'action="{issue}"' in _detail_body(
        _firm_session(invited_firm.owner),
        invited_firm,
    )
    issued = _firm_session(invited_firm.owner).post(
        issue,
        {"email": INVITED_EMAIL, "role": TenantRole.CLIENT_OWNER},
        headers={"host": INVITE_FIRM_HOST},
    )
    assert issued.status_code == HTTPStatus.FOUND, (
        f"issuing the invitation answered {issued.status_code} rather than redirecting "
        f"to the confirmation the firm-side invite flow already ends on"
    )
    assert issued["Location"] == reverse("invite-issued")

    # When the invited owner opens the link that reached their mailbox, holding no
    # account and no session at all
    accept = _accept_path_from_outbox()
    invitee = Client()
    opened = invitee.get(accept, headers={"host": PORTAL_HOST})

    # Then the acceptance page is served on the portal host, drawn by THIS project's
    # template on the entrance shell. Status alone is satisfied by upstream markup and
    # by the unstyled placeholder this page replaced, so the origin, the title and the
    # stylesheet are asserted with it.
    assert opened.status_code == HTTPStatus.OK, (
        f"the acceptance link answered {opened.status_code} on {PORTAL_HOST}; "
        f"{opened.get('Location', 'no Location header')}"
    )
    landing = opened.content.decode()
    assert _origin_of(opened, ACCEPT_TEMPLATE, "accept").is_relative_to(
        PROJECT_TEMPLATES,
    ), f"{ACCEPT_TEMPLATE} is not this project's file"
    assert STYLE_MARKER in landing, (
        f"the acceptance page rendered without {STYLE_MARKER}, so no layout of ours "
        f"ran and the first screen a MEI owner sees is bare markup"
    )
    assert f"<title>{ACCEPT_LABEL}</title>" in landing
    assert len(H1.findall(landing)) == 1, (
        f"the acceptance page renders {len(H1.findall(landing))} top-level headings; "
        f"the entrance layout owns exactly one"
    )
    assert INVITED_EMAIL in landing, (
        "the acceptance page does not name the address the invitation was issued to, "
        "so the reader cannot tell whether the link is theirs before typing a password"
    )
    assert invited_firm.tenant.name in landing, (
        "the acceptance page does not name the firm that issued the invitation"
    )

    # When they set a password
    submitted = invitee.post(
        accept,
        {"full_name": "Dona Maria", "password1": PASSWORD, "password2": PASSWORD},
        headers={"host": PORTAL_HOST},
    )
    assert submitted.status_code == HTTPStatus.FOUND, (
        f"setting the password answered {submitted.status_code} rather than signing "
        f"the new owner in and sending them onward"
    )
    home = reverse("portal-home", urlconf=PORTAL_URLCONF)
    assert submitted["Location"] == home

    # Then the enrolment gate takes over before any page renders, exactly as it does
    # for the three legs above, and the walk ends on the activation screen
    gate = _walk(
        invitee,
        Leg(
            label="invite",
            entry=home,
            terminal=_path("mfa_activate_totp"),
            page_template="mfa/totp/activate_form.html",
            title="Ativar autenticador",
            least_redirects=1,
        ),
    )

    # When they enrol with a code their authenticator would produce from the secret
    # this very page handed them
    with pinned_totp_window():
        enrolled = invitee.post(
            _path("mfa_activate_totp"),
            {"code": totp_code(_secret_from(gate.content.decode()))},
            headers={"host": PORTAL_HOST},
        )
    assert enrolled.status_code in REDIRECTED, (
        f"the authenticator code was refused ({enrolled.status_code}), so enrolment "
        f"never completed and the landing page below would be the gate again"
    )

    # Then Início is theirs, on their own firm's portal host, and the gate lets them
    # through without diverting at all
    inicio = _walk(
        invitee,
        Leg(
            label="inicio",
            entry="/",
            terminal=home,
            page_template=PORTAL_HOME_TEMPLATE,
            title=PORTAL_TITLE,
            least_redirects=0,
        ),
    )
    arrived = inicio.content.decode()
    headings = [found.strip() for found in HEADING.findall(arrived)]
    assert headings == [company.legal_name], (
        f"the landing page's heading is {headings} rather than "
        f"[{company.legal_name!r}]; the invitee reached Início without their own "
        f"company on it, which is the one thing this seat was granted for"
    )
    bar = arrived[arrived.index("<nav") :]
    assert CURRENT_PAGE in bar[: bar.index(INICIO)], (
        f"the portal bar does not mark {INICIO} as the page being viewed, so the "
        f"invitee landed somewhere other than the portal's own home"
    )
    assert _origin_of(inicio, PORTAL_HOME_TEMPLATE, "inicio").is_relative_to(
        PORTAL_TEMPLATE_ROOT,
    ), "the landing page is not the shipped portal template"

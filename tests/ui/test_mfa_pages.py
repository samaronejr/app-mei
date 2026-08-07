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
"""

import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole

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

"""The seven allauth account screens this project overrides — and the closed signup.

`tests/ui/test_auth_templates.py` already refuses the firm shell and proves the login
page survives the portal host. It says nothing about the other screens, and
nothing at all about whether a page this project overrode actually *reached* a
document: a template that raises inside a `{% block %}` and one that was never written
both leave allauth's own file rendering, and allauth ships a compiled pt-BR catalogue,
so the stock screen answers 200 in Portuguese and looks approximately right. Every
positive claim below is therefore paired with an origin check — the loader is asked
which file it resolved — because status and language cannot tell the two apart.

The negative half is the point of the file. Six screens under `account/` carry `mail`
in their filename, and `tests/scope/test_scope_fidelity.py::test_row_3_no_notifications`
asserts that `templates/` holds ZERO such files: notifications are Phase 2, and a
project-owned mail template is the first brick of it. Those six are named here, each
asserted to resolve out of site-packages, and one of them is rendered end to end to
show what the abstention costs — nothing, because they inherit
`templates/allauth/layouts/base.html` through allauth's own two-line entrance file and
come out styled without a byte of ours in between.

The rendered-heading count is asserted per screen rather than once, for the same reason
`tests/ui/test_layout.py` counts it on real pages as well as scanning source: the
override contributes its title through the `h1` element, which this project renders as
a paragraph, and a page that grew a real heading tag would give the document two.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from django.conf import settings
from django.core import mail
from django.template.backends.django import Template as BackendTemplate
from django.template.loader import get_template
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

# The project template directory, read from settings so that moving the tree cannot
# leave this comparing paths nobody serves.
PROJECT_TEMPLATES: Final[Path] = Path(settings.TEMPLATES[0]["DIRS"][0])

# The top-level heading, counted on rendered documents. Same expression as
# `tests/ui/test_layout.py` uses, because it is the same promise.
H1: Final = re.compile(r"<h1[\s>]", re.IGNORECASE)

# The compiled stylesheet. Stock allauth links no stylesheet at all, so this byte
# string is present only when one of THIS project's layouts rendered the page.
STYLE_MARKER: Final = "css/app.css"
CLOSED_SIGNUP_TEMPLATE: Final = "account/signup_closed.html"

# The announced error summary `templates/partials/pure/form_errors.html` emits. The
# container's tag is captured rather than assumed, so a summary rendered as a
# `<section>` still reads.
SUMMARY: Final = re.compile(
    r"""<(\w+)\b[^>]*\bclass\s*=\s*["'][^"']*\bform-errors\b[^"']*["'][^>]*>""",
)
ALERT_ROLE: Final = 'role="alert"'

# The reset link, matched as a path so the assertion does not depend on which host
# built the absolute URL in the mail body.
RESET_PATH: Final = re.compile(r"/accounts/password/reset/key/[\w-]+/")

REDIRECTED: Final = range(300, 400)

FIRM_SLUG: Final = "alfa-entrada"
WRONG_PASSWORD: Final = "nao-e-esta-senha"  # noqa: S105

# The six screens that stay upstream's. Every one of them carries `mail` in its
# filename — "e-mail" contains it too — and row 3 of the scope table asserts
# `templates/` holds no such file.
UPSTREAM_ONLY: Final[tuple[str, ...]] = (
    "account/email.html",
    "account/email_confirm.html",
    "account/email_change.html",
    "account/verified_email_required.html",
    "account/base_manage_email.html",
    "account/confirm_email_verification_code.html",
)

# The one of those six that this project's own flow walks through on every signup,
# `ACCOUNT_EMAIL_VERIFICATION` being "mandatory". Rendered below to show the
# abstention is free.
VERIFICATION_SENT: Final = "account_email_verification_sent"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    """One firm, with an owner who is enrolled in TOTP and can actually sign in."""
    return make_firm(FIRM_SLUG)


def _anonymous(firm: Firm) -> Client:
    return Client(SERVER_NAME=firm.host)


# ------------------------------------------------------------------ reaching each page


def _login(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    return _anonymous(firm).get(reverse("account_login"))


def _closed_signup(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    return _anonymous(firm).get(reverse("account_signup"))


def _logout(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    return firm.as_owner().get(reverse("account_logout"))


def _password_reset(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    return _anonymous(firm).get(reverse("account_reset_password"))


def _password_reset_done(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    return _anonymous(firm).get(reverse("account_reset_password_done"))


def _password_reset_from_key(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    """Walk the real reset: request one, read the mail, follow the link it carries.

    The live key rather than a fabricated one, because the two halves of this template
    are selected by `token_fail` and only a genuine key reaches the branch that holds
    the form. `_password_reset_from_key_dead` below covers the other.
    """
    client = _anonymous(firm)
    requested = client.post(
        reverse("account_reset_password"),
        {"email": firm.owner.email},
    )
    assert requested.status_code in REDIRECTED, (
        f"the reset request answered {requested.status_code} instead of redirecting to "
        f"the receipt, so no mail was sent and there is no link to follow"
    )
    assert mail.outbox, "the reset request sent no mail, so there is no key to use"

    # `str()` because a message body is typed as a lazy string promise, which the
    # regex module will not accept until it has been resolved.
    sent = str(mail.outbox[-1].body)
    found = RESET_PATH.search(sent)
    assert found is not None, (
        f"no reset path in the mail body, so the link a customer clicks cannot be "
        f"exercised: {sent!r}"
    )
    # allauth redirects the key out of the URL before rendering, to keep it off the
    # Referer header, so the form lives one hop further on.
    return client.get(found.group(0), follow=True)


def _password_reset_from_key_dead(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    """A spent or forged key, which is the ordinary way to reach this screen twice."""
    return _anonymous(firm).get(
        reverse(
            "account_reset_password_from_key",
            kwargs={"uidb36": "0", "key": "chave-que-nao-existe"},
        ),
    )


def _password_reset_from_key_done(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    return _anonymous(firm).get(reverse("account_reset_password_from_key_done"))


def _password_change(firm: Firm) -> "_MonkeyPatchedWSGIResponse":
    return firm.as_owner().get(reverse("account_change_password"))


@dataclass(frozen=True)
class Screen:
    """One overridden page: the file, the title it sets, and how to reach it."""

    template: str
    title: str
    reach: Callable[[Firm], "_MonkeyPatchedWSGIResponse"]


# The seven overrides, each paired with the title its own `head_title` block writes.
# `password_reset_from_key.html` appears twice because one file renders two screens and
# a suite that only ever saw the happy one would not notice the other going blank.
SCREENS: Final[tuple[Screen, ...]] = (
    Screen("account/login.html", "Entrar", _login),
    Screen("account/logout.html", "Sair", _logout),
    Screen("account/password_reset.html", "Redefinir senha", _password_reset),
    Screen(
        "account/password_reset_done.html",
        "Link enviado",
        _password_reset_done,
    ),
    Screen(
        "account/password_reset_from_key.html",
        "Definir nova senha",
        _password_reset_from_key,
    ),
    Screen(
        "account/password_reset_from_key.html",
        "Link inválido",
        _password_reset_from_key_dead,
    ),
    Screen(
        "account/password_reset_from_key_done.html",
        "Senha alterada",
        _password_reset_from_key_done,
    ),
    Screen("account/password_change.html", "Alterar senha", _password_change),
)


def _identify(screen: Screen) -> str:
    return f"{screen.template}::{screen.title}"


def _resolved(name: str) -> Path:
    """Return the file the loader actually hands back for a template name.

    Two unwrappings, and both are load-bearing. `get_template` returns the BACKEND's
    template — a thin adapter with no origin of its own — and the compiled Django
    template it carries is what remembers which file it was read from. Asking the
    adapter for `.origin` type-checks against nothing and is the shape of this helper
    that would answer `None` for every name and quietly pass the whole file.
    """
    backend = get_template(name)
    assert isinstance(backend, BackendTemplate), (
        f"{name} was loaded by {type(backend).__name__} rather than Django's own "
        f"backend, so it carries no compiled template and no origin to compare"
    )
    origin = backend.template.origin.name
    assert origin, (
        f"{name} resolved to a template with no origin on disk, so nothing here can "
        f"say whether it came from this project or from site-packages"
    )
    return Path(origin)


# --------------------------------------------------------- (i) the overrides, rendered


@pytest.mark.parametrize("screen", SCREENS, ids=_identify)
def test_each_overridden_account_page_renders_styled_with_one_heading(
    firm: Firm,
    screen: Screen,
) -> None:
    # Given one of the eight screens this project took over
    # When it is reached the way a person reaches it
    response = screen.reach(firm)

    # Then it is a rendered document and not a redirect, a 403 or a NoReverseMatch.
    assert response.status_code == HTTPStatus.OK, (
        f"{screen.template} answered {response.status_code}; "
        f"{response.get('Location', 'no Location header')}"
    )
    body = response.content.decode()

    # And one of this project's layouts drew it. Status alone would be satisfied by
    # allauth's stock document, which ships a compiled pt-BR catalogue and therefore
    # answers 200 in Portuguese.
    assert STYLE_MARKER in body, (
        f"{screen.template} rendered without {STYLE_MARKER}: no layout of ours ran"
    )

    # And it carries exactly one top-level heading. The layout owns it; the card's own
    # title goes through the `h1` element, which this project renders as a paragraph.
    assert len(H1.findall(body)) == 1, (
        f"{screen.template} rendered {len(H1.findall(body))} top-level headings; the "
        f"layout owns exactly one and the page title is a paragraph beneath it"
    )

    # And the title is the one this override writes, not upstream's translation.
    assert f"<title>{screen.title}</title>" in body, (
        f"{screen.template} did not set its own head_title to {screen.title!r}"
    )


@pytest.mark.parametrize(
    "template",
    sorted({screen.template for screen in SCREENS}),
)
def test_each_overridden_page_resolves_to_this_project_and_not_site_packages(
    template: str,
) -> None:
    """The loader must prefer our file, or every claim above is about allauth's.

    allauth's own screens answer 200, link no stylesheet and render one heading — so
    the render above would catch a page that vanished, but not one that was never
    overridden in the first place while some other layout supplied the marker.
    """
    resolved = _resolved(template)
    assert resolved.is_relative_to(PROJECT_TEMPLATES), (
        f"{template} resolves to {resolved}, which is not under {PROJECT_TEMPLATES}: "
        f"the override was never written, or it is shadowed by an earlier loader"
    )


# ------------------------------------------------------- (ii) the rejection, announced


def test_a_rejected_sign_in_comes_back_with_an_announced_summary(firm: Firm) -> None:
    """A wrong password re-renders the form carrying a summary a screen reader hears.

    The failure allauth produces here is a NON-field error — enumeration prevention
    means it refuses to say which half was wrong — so the summary is the only place the
    rejection is stated at all. A page that dropped `{% element fields %}` in favour of
    hand-rolled controls would still show the form and still answer 200.
    """
    # Given the firm owner's real address and a password that is not theirs
    response = _anonymous(firm).post(
        reverse("account_login"),
        {"login": firm.owner.email, "password": WRONG_PASSWORD},
    )

    # Then the form comes back rather than redirecting, and rather than being refused
    # by the credential limiter — one attempt is well inside RATELIMIT_LOGIN_EMAIL.
    assert response.status_code == HTTPStatus.OK, (
        f"the rejected sign-in answered {response.status_code} instead of re-rendering "
        f"the form with its errors"
    )
    body = response.content.decode()

    # And the summary is present and announced.
    assert SUMMARY.search(body) is not None, (
        "the rejected sign-in rendered no .form-errors summary, so the only statement "
        "of what went wrong is invisible to anyone not looking at the field"
    )
    assert ALERT_ROLE in body, (
        "the summary is not an alert region, so it is never announced and a screen "
        "reader user has to hunt the page for the reason the sign-in failed"
    )


def test_the_closed_signup_uses_upstream_refusal_inside_the_project_layout(
    firm: Firm,
) -> None:
    response = _closed_signup(firm)

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    rendered = [str(template.name) for template in response.templates]
    assert CLOSED_SIGNUP_TEMPLATE in rendered, (
        f"the signup route did not render {CLOSED_SIGNUP_TEMPLATE}: {rendered}"
    )
    assert "account/signup.html" not in rendered, (
        "the signup route rendered the dormant public form instead of the refusal"
    )
    assert STYLE_MARKER in body
    assert "<form" not in body.lower()


# ------------------------------------------- (iii) the six this project does not write


@pytest.mark.parametrize("template", UPSTREAM_ONLY)
def test_the_address_screens_are_left_to_allauth(template: str) -> None:
    """Row 3 forbids a project-owned template whose filename carries `mail`.

    `tests/scope/test_scope_fidelity.py::test_row_3_no_notifications` scans
    `templates/` and asserts zero such files — notifications are Phase 2, and the two
    mails this scope does send are built inline from a literal body. Overriding any of
    the six below would turn that guard red, so the abstention is asserted from this
    side too: the file is not ours, and it is not merely missing from a stale list.
    """
    resolved = _resolved(template)
    assert not resolved.is_relative_to(PROJECT_TEMPLATES), (
        f"{template} now resolves to {resolved}, inside {PROJECT_TEMPLATES}. Its "
        f"filename carries `mail`, which row 3 of the scope table refuses outright"
    )


def test_the_address_verification_screen_is_styled_without_being_overridden(
    firm: Firm,
) -> None:
    """What the abstention costs: nothing.

    `ACCOUNT_EMAIL_VERIFICATION` is "mandatory", so this is the screen every new
    account lands on. It is allauth's file, extending allauth's two-line entrance
    layout, whose parent the loader resolves to `templates/allauth/layouts/base.html`
    — this project's. So it arrives in the card, with the stylesheet and one heading,
    and no file of ours in between.
    """
    response = _anonymous(firm).get(reverse(VERIFICATION_SENT))

    assert response.status_code == HTTPStatus.OK, (
        f"{VERIFICATION_SENT} answered {response.status_code}"
    )
    body = response.content.decode()
    assert STYLE_MARKER in body, (
        f"{VERIFICATION_SENT} rendered without {STYLE_MARKER}, so the inherited layout "
        f"did not run and the six unoverridden screens are unstyled after all"
    )
    assert len(H1.findall(body)) == 1, (
        f"{VERIFICATION_SENT} rendered {len(H1.findall(body))} top-level headings"
    )


def test_no_project_template_carries_mail_in_its_filename() -> None:
    """Row 3's own claim, restated where this wave would break it.

    Duplicated deliberately. Row 3 lives in the scope suite and reads as a statement
    about notifications; the wave most likely to violate it is this one, and a failure
    here names the reason in the same file as the six templates it was tempted by.
    """
    offenders = sorted(
        str(path.relative_to(PROJECT_TEMPLATES))
        for path in PROJECT_TEMPLATES.rglob("*.html")
        if re.search(r"mail", path.name, re.IGNORECASE)
    )
    assert offenders == [], (
        f"row 3 of the scope table forbids a project-owned template whose filename "
        f"carries `mail`, and this wave added {offenders}"
    )

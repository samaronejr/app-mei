"""What the interface does when the request behind it fails, expires, or repeats.

Four legs, each guarding a different way an HTMX-enhanced page stops behaving like a
page. They are grouped because they share one premise: *an HTMX swap is not a
navigation*, so every browser behaviour that navigation gives away for free has to be
re-established by hand or deliberately declined.

**The expired session.** `login_required` answers an expired session with a 302 to the
login screen, which is exactly right for a navigation and exactly wrong for a swap.
`XMLHttpRequest` follows redirects transparently, so HTMX never sees the 302 — it sees
the login page, at 200, and swaps the whole sign-in form into whatever fragment the
accountant was looking at. The result is a login form nested inside a stale dashboard,
with no address bar change to explain it and no way to submit it correctly. The fix is
`HX-Redirect`, which HTMX turns into a real `window.location` navigation, and it is
asserted here through a genuinely expired session rather than a hand-set header,
because the acceptance criterion is about session expiry and not about a header.

**hx-sync.** Every element that issues an HTMX request into a shared target can have
two of its requests in flight at once, and the responses land in whatever order the
network settles on. Under navigation the browser cancels the previous request for
free; under HTMX nothing does, so the last response to arrive wins the target even
when it answers the older question. `hx-sync` is what declines that race, and the rule
below quantifies over *every* request-issuing element rather than the two filter forms
that first needed it, so the next one added inherits the guard instead of the bug.

**The failed partial.** A 500 on a swap is silent: no browser error page, no
navigation, just a region that never changes. `#htmx-error-fallback` plus the
`htmx:responseError` handler in `static/js/app.js` are the two halves of saying so out
loud, and both are asserted because either alone is inert — markup nothing reveals, or
a handler with nothing to reveal. Only their *presence* is checked here; that a real
browser reveals it on a real 500 belongs to the browser leg of todo 35.

**Double submit.** The guarantee that a filing is not recorded twice is server-side
Post/Redirect/Get, and it is asserted where PRG is asserted. The disable-on-submit
handler below it is presentation: it stops the second click from being *made*, so the
accountant is not left wondering which of two identical requests won. Nothing here
tests it as protection, because it is not protection — a client with no JavaScript,
or a forged POST, meets the same PRG and is answered the same way.

Every scan enters through a helper that refuses to return a result proving nothing --
a page that never rendered, a file that is missing or empty, a scan that matched no
elements at all. Those gates live inside the helpers rather than in tests of their
own, because `pytest -k` deselects a test and the rules above it would then quantify
over emptiness and pass while measuring nothing.
"""

import re
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.test import Client, RequestFactory
from django.urls import reverse
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from apps.security.middleware import RateLimitMiddleware
from tests.ui.factories import Firm, make_firm
from tests.ui.templates_scan import loaded_templates, template_files

pytestmark = pytest.mark.django_db(transaction=True)

APP_JS: Final[Path] = Path(settings.BASE_DIR) / "static" / "js" / "app.js"

# The header HTMX puts on every request it issues. Read from the request rather than
# inferred from `X-Requested-With`, which jQuery and several other libraries also set.
HX_REQUEST: Final[str] = "HX-Request"
HX_REDIRECT: Final[str] = "HX-Redirect"

# The fallback block in templates/base.html, and the class pair that makes it read as
# an error rather than as neutral chrome.
FALLBACK_ID: Final[str] = "htmx-error-fallback"
NOTICE_CLASSES: Final[tuple[str, ...]] = ("notice", "notice-error")

# The handler side of the same contract: the event HTMX fires on a 5xx, and the call
# that takes `hidden` back off the block above.
RESPONSE_ERROR_EVENT: Final[str] = "htmx:responseError"
REVEAL_CALL: Final[str] = 'removeAttribute("hidden")'

# The double-submit enhancement, and the note naming what actually guarantees the
# invariant. The note is asserted so the file cannot quietly start reading as though
# the script were the protection.
SUBMIT_LISTENER: Final[str] = 'addEventListener("submit"'
DISABLE_CALL: Final[str] = "disabled"
PRG_NOTE: Final[str] = "Post/Redirect/Get"

# A `{% comment %}` block. Stripped before the markup scan below, unlike
# `templates_scan.class_tokens`, which reads them on purpose: a class name written in
# a comment still has to be compiled by Tailwind, whereas an `hx-get` written in one
# is prose about markup and not markup.
COMMENT_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
    re.DOTALL,
)

# One opening tag. `[^<>]*` cannot run away past the tag it started in, and no
# attribute in this project carries a bare angle bracket.
OPEN_TAG: Final[re.Pattern[str]] = re.compile(
    r"<[a-zA-Z][\w-]*(?:\s[^<>]*)?>",
    re.DOTALL,
)

# An attribute name on a tag, read positionally rather than as a pair, because
# `hx-boost="true"` and a valueless attribute have to be found the same way.
ATTRIBUTE_NAME: Final[re.Pattern[str]] = re.compile(r"(?:^|\s)([a-zA-Z_@:][\w:.@-]*)")

# Every attribute that makes an element issue an HTTP request through HTMX. `hx-boost`
# is in the list because a boosted container turns its descendant links and forms into
# HTMX requests, which is the same race by a different spelling.
REQUEST_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "hx-get",
        "hx-post",
        "hx-put",
        "hx-patch",
        "hx-delete",
        "hx-boost",
    },
)
SYNC_ATTRIBUTE: Final[str] = "hx-sync"

# The two filter forms this rule was written for. Named so the scan cannot pass by
# finding nothing: a refactor that moves them still has to keep them covered.
KNOWN_FILTER_FORMS: Final[tuple[str, ...]] = (
    "core/dashboard.html",
    "obligations/queue.html",
)

# The portal renders under a role holding SELECT on seven tables and ships no
# JavaScript at all, so it has no HTMX to synchronise. Excluded by path rather than by
# absence, so that an hx-* attribute appearing there fails this scan's premise loudly.
PORTAL_MARKER: Final[str] = "/portal/templates/"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("alpha-resilience")


def login_path() -> str:
    """The path `login_required` sends an unauthenticated caller to."""
    return reverse(settings.LOGIN_URL)


def app_js_source() -> str:
    """Return `static/js/app.js`, refusing to hand back a file that proves nothing."""
    assert APP_JS.is_file(), (
        f"{APP_JS} does not exist; every assertion about the firm-side script would "
        "otherwise be quantifying over a missing file"
    )
    source = APP_JS.read_text(encoding="utf-8")
    assert source.strip(), f"{APP_JS} is empty; nothing below is measuring behaviour"
    return source


def firm_page(firm: Firm) -> str:
    """Render a signed-in firm screen, gating on it actually having rendered."""
    response = firm.as_owner().get(reverse("dashboard"))
    assert response.status_code == HTTPStatus.OK, (
        f"the dashboard returned {response.status_code} rather than 200; "
        "nothing below is measuring the firm layout"
    )
    return response.content.decode()


def expired_session_client(firm: Firm) -> Client:
    """Sign in for real, then expire the session the way time expires it.

    The expiry is written through the session store rather than by deleting the row,
    so the request that follows travels the same path a session left open overnight
    does: the cookie is still sent, `SessionStore.load` finds no row whose
    `expire_date` is still in the future, and the caller arrives anonymous.

    Both gates are here rather than in a test of their own. Without the first, a
    fixture that never authenticated would make the expiry meaningless; without the
    second, a session that never actually expired would make the whole leg vacuous.
    """
    client = firm.as_owner()
    live = client.get(reverse("dashboard"))
    assert live.status_code == HTTPStatus.OK, (
        f"the signed-in dashboard returned {live.status_code}; the session under test "
        "was never valid, so expiring it measures nothing"
    )

    session = client.session
    session.set_expiry(-1)
    session.save()

    stale = client.get(reverse("dashboard"))
    assert stale.status_code == HTTPStatus.FOUND, (
        f"an expired session still returned {stale.status_code} on an ordinary "
        "navigation; the session did not expire and the rest of this leg is vacuous"
    )
    assert login_path() in stale.headers["Location"], (
        f"an expired session redirected to {stale.headers['Location']!r} rather than "
        "to the login screen"
    )
    return client


def request_issuing_tags() -> list[tuple[Path, str]]:
    """Return every firm-side opening tag that issues an HTMX request."""
    found: list[tuple[Path, str]] = []
    for path, text in loaded_templates():
        if PORTAL_MARKER in path.as_posix():
            continue
        markup = COMMENT_BLOCK.sub(" ", text)
        for tag in OPEN_TAG.findall(markup):
            names = {name.lower() for name in ATTRIBUTE_NAME.findall(tag)}
            if names & REQUEST_ATTRIBUTES:
                found.append((path, tag))
    assert found, (
        "no firm-side element issues an HTMX request; the hx-sync rule below would "
        "quantify over nothing and pass while measuring nothing"
    )
    return found


def test_the_scan_reaches_every_known_filter_form() -> None:
    """Gate the rule below on the templates it exists to cover being reachable."""
    scanned = {path.as_posix() for path, _ in loaded_templates()}
    for name in KNOWN_FILTER_FORMS:
        assert any(candidate.endswith(name) for candidate in scanned), (
            f"{name} was not reached by the template scan; the hx-sync rule cannot "
            "be covering the filter form it was written for"
        )
    assert template_files(), "the template scan found no files at all"


def test_an_expired_session_answers_htmx_with_hx_redirect(
    firm: Firm,
) -> None:
    """HTMX must be told to navigate, not handed a login page to swap in.

    The status assertion is the load-bearing one. `XMLHttpRequest` follows a 3xx
    itself, so a response that stays a redirect never reaches HTMX with its headers
    intact — the browser resolves it and HTMX swaps the login screen into the
    fragment. Only a non-redirect status delivers `HX-Redirect` to the client at all.
    """
    client = expired_session_client(firm)

    response = client.get(reverse("dashboard"), headers={HX_REQUEST: "true"})

    assert HX_REDIRECT in response.headers, (
        f"an expired session answered an HTMX request with {response.status_code} and "
        f"no {HX_REDIRECT}; HTMX would follow the redirect and swap the login screen "
        "into the fragment the accountant was reading"
    )
    redirected = (
        HTTPStatus.MULTIPLE_CHOICES <= response.status_code < HTTPStatus.BAD_REQUEST
    )
    assert not redirected, (
        f"the response is still a {response.status_code} redirect; XMLHttpRequest "
        f"follows it transparently, so {HX_REDIRECT} never reaches HTMX"
    )
    assert response.status_code == HTTPStatus.NO_CONTENT, (
        f"expected 204 with the redirect carried in {HX_REDIRECT}, got "
        f"{response.status_code}"
    )

    destination = response.headers[HX_REDIRECT]
    assert destination.startswith(login_path()), (
        f"{HX_REDIRECT} points at {destination!r} rather than at the login screen"
    )
    assert reverse("dashboard") in destination, (
        f"{HX_REDIRECT} is {destination!r} and carries no way back to the page the "
        "accountant was on; signing in would drop them somewhere else"
    )
    assert not response.content, (
        "the rewritten response carries a body; there is nothing for HTMX to swap "
        "and anything present would be swapped"
    )


def test_an_ordinary_navigation_still_receives_the_plain_redirect(
    firm: Firm,
) -> None:
    """The rewrite is scoped to HTMX. A browser navigation keeps its 302."""
    client = expired_session_client(firm)

    response = client.get(reverse("dashboard"))

    assert response.status_code == HTTPStatus.FOUND, (
        f"a plain navigation received {response.status_code}; the rewrite is not "
        "scoped to HTMX requests and ordinary sign-in has been broken"
    )
    assert HX_REDIRECT not in response.headers, (
        f"a plain navigation carries {HX_REDIRECT}, which nothing reads"
    )


def test_a_successful_htmx_request_is_left_alone(firm: Firm) -> None:
    """A live session gets its fragment, not a redirect header."""
    response = firm.as_owner().get(reverse("dashboard"), headers={HX_REQUEST: "true"})

    assert response.status_code == HTTPStatus.OK, (
        f"a signed-in HTMX request returned {response.status_code}; the rewrite is "
        "firing on responses that are not the login redirect"
    )
    assert HX_REDIRECT not in response.headers, (
        f"a successful fragment carries {HX_REDIRECT}; HTMX would navigate away from "
        "the page instead of swapping the content it just asked for"
    )


def test_the_rewrite_issues_no_queries(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    """It runs inside the portal transaction, where a query is a permission error.

    `RateLimitMiddleware` is registered inside the transaction `PortalMiddleware`
    opens, under a role holding SELECT on seven tables. A rewrite that read anything
    -- a session, a membership, a site row -- would turn every expired portal request
    into a 500 rather than a redirect. Asserted directly rather than inferred from a
    page's query budget, where the session and tenant reads would hide it.
    """
    login = login_path()
    factory = RequestFactory()
    request: HttpRequest = factory.get(
        "/painel/",
        headers={HX_REQUEST: "true"},
    )

    def _redirect(_request: HttpRequest) -> HttpResponseBase:
        return HttpResponseRedirect(f"{login}?next=/painel/")

    middleware = RateLimitMiddleware(_redirect)

    with django_assert_num_queries(0):
        response = middleware(request)

    assert isinstance(response, HttpResponse)
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert response.headers[HX_REDIRECT] == f"{login}?next=/painel/"


def test_every_htmx_request_issuing_element_declines_the_race() -> None:
    """Every firm-side element that issues an HTMX request carries `hx-sync`.

    Quantified over request-issuing elements rather than over forms, because the
    race is a property of issuing a request into a target somebody else can also
    write -- a boosted pagination container and a polling counter strip produce it
    exactly as a filter form does, and only the filter forms were ever fixed by hand.
    """
    offenders = [
        (path.as_posix(), tag)
        for path, tag in request_issuing_tags()
        if SYNC_ATTRIBUTE not in {name.lower() for name in ATTRIBUTE_NAME.findall(tag)}
    ]

    assert not offenders, (
        "these elements issue HTMX requests with no hx-sync, so two of their "
        "responses can land out of order and the later question loses the target:\n"
        + "\n".join(f"  {path}: {tag.strip()}" for path, tag in offenders)
    )


def test_the_failed_partial_has_something_to_reveal(firm: Firm) -> None:
    """The error notice is in the markup of every firm page, hidden until needed."""
    page = firm_page(firm)

    assert f'id="{FALLBACK_ID}"' in page, (
        f"no #{FALLBACK_ID} block on the firm layout; a 500 on a swap would leave a "
        "region that silently never changes"
    )
    for token in NOTICE_CLASSES:
        assert token in page, (
            f"the fallback block does not carry {token!r}, so a failure would render "
            "as neutral chrome rather than as an error"
        )
    assert "hidden" in page, (
        f"#{FALLBACK_ID} is not hidden on a healthy page; the notice would be showing "
        "an error that has not happened"
    )


def test_the_failed_partial_has_something_that_reveals_it() -> None:
    """`app.js` listens for the 5xx and takes `hidden` back off the notice."""
    source = app_js_source()

    assert RESPONSE_ERROR_EVENT in source, (
        f"{APP_JS.name} does not listen for {RESPONSE_ERROR_EVENT}; the notice above "
        "is markup nothing ever reveals"
    )
    assert REVEAL_CALL in source, (
        f"{APP_JS.name} never calls {REVEAL_CALL}, so the hidden notice stays hidden"
    )
    assert FALLBACK_ID in source, (
        f"{APP_JS.name} does not name {FALLBACK_ID}; the handler and the markup are "
        "not wired to each other"
    )


def test_a_repeated_submit_is_declined_in_the_interface() -> None:
    """A second click is stopped from being made. It is not what stops it counting.

    Server-side Post/Redirect/Get is the guarantee, and it is asserted where PRG is
    asserted. This leg only checks that the enhancement exists and that the file says
    so, because a script that reads as the protection is one refactor away from
    somebody deleting the redirect it was standing in front of.
    """
    source = app_js_source()

    assert SUBMIT_LISTENER in source, (
        f"{APP_JS.name} does not listen for form submission; a double click sends two "
        "identical POSTs and the accountant sees no sign which one was answered"
    )
    assert DISABLE_CALL in source, (
        f"{APP_JS.name} listens for submission without disabling anything"
    )
    assert PRG_NOTE in source, (
        f"{APP_JS.name} does not name {PRG_NOTE!r} as what actually makes a repeated "
        "submit harmless; the next reader will take the script for the guarantee"
    )

"""Feedback surfaces: the status banner, the announcer, and the script behind them.

Three legs, each guarding a different way this interface stops telling anyone what
just happened.

A message set with `django.contrib.messages` has to land in a live region or the
confirmation that a filing went through is visible only to someone already looking at
the part of the page it appeared in. `role="status"` says what the element *is*;
`aria-live="polite"` says announce it at the next pause. Both, because the implicit
live-region semantics of `role="status"` are honoured inconsistently across screen
readers, and `aria-live` on its own leaves the element with no role to announce it as.

The assertive region is the other half. HTMX swaps content without a navigation, so
nothing tells assistive technology the page changed; `#htmx-announce` is the single
element `static/js/app.js` writes those announcements into. Exactly one, because
`aria-live="assertive"` interrupts whatever is being read -- a second region is a
second interruption -- and because two of them race, with the loser dropped silently.

`static/js/app.js` is the first first-party script this project ships. It is checked
here rather than only in the asset pipeline because of the policy it has to live
under: `apps/security/csp.py` sets `script-src 'self'` with no `'unsafe-eval'`, and
`allowEval: false` in the htmx-config meta means an `hx-on:` handler added to this
file would stop working at runtime rather than fail at load.

Every scan enters through a helper that refuses to return a result proving nothing --
a parse that never reached the layout, a file that does not exist, a file that is
empty. Those gates live in the helpers rather than in a test of their own on purpose:
`pytest -k` deselects a test, and the rules underneath it would then quantify over
emptiness and pass.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

import pytest
from django.conf import settings
from django.contrib import messages
from django.contrib.messages.storage import default_storage
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from tests.ui.factories import Firm, make_firm
from tests.ui.templates_scan import EXTERNAL_URL

# The loader root, so the scanned file is the one the engine resolves by name.
TEMPLATE_ROOT: Final[Path] = Path(settings.TEMPLATES[0]["DIRS"][0])
BASE_TEMPLATE: Final[str] = "base.html"
BASE_SOURCE: Final[Path] = TEMPLATE_ROOT / BASE_TEMPLATE

# The logical name `{% static %}` is given, and the file on disk it resolves to.
APP_JS_LOGICAL: Final[str] = "js/app.js"
APP_JS: Final[Path] = Path(settings.BASE_DIR) / "static" / APP_JS_LOGICAL
STATIC_URL: Final[str] = str(settings.STATIC_URL)

ANNOUNCE_ID: Final[str] = "htmx-announce"

# Accented and punctuated the way a real confirmation is, so that an assertion which
# only holds for ASCII is caught here rather than by a user.
MESSAGE: Final[str] = "Declaração enviada com sucesso."

# `{% static 'js/app.js' %}` in either quote style. Asserted against the template
# source rather than the render because `/static/js/app.js` written by hand renders
# identically today and silently skips the manifest hashing in production.
STATIC_TAG: Final[re.Pattern[str]] = re.compile(
    r"\{%\s*static\s+(['\"])js/app\.js\1\s*%\}",
)

# The three constructs the policy forbids, named individually so a failure says which
# one it found instead of only that something matched.
FORBIDDEN_JS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("eval(", re.compile(r"\beval\s*\(")),
    ("new Function", re.compile(r"\bnew\s+Function\b")),
    ("hx-on:", re.compile(r"hx-on:")),
)

# HTML5 elements that never receive an end tag. Closed on sight below; left open they
# would make `<meta charset>` an ancestor of everything after it in the document.
VOID_ELEMENTS: Final[frozenset[str]] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    },
)


@dataclass(frozen=True)
class Element:
    """One element from a rendered document: its tag, attributes and subtree text."""

    tag: str
    attrs: dict[str, str]
    text: str


class _Collector(HTMLParser):
    """Collect every element, with the text of its whole subtree.

    Text is appended to every element currently open rather than only the innermost
    one, so `text` answers "does this region contain the message" -- which is the
    question a live region is for. A regex cannot answer it: the announcing element
    is an ancestor of the paragraph the words are actually in.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._done: list[Element] = []
        self._open: list[tuple[str, dict[str, str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pairs = {name: value or "" for name, value in attrs}
        if tag in VOID_ELEMENTS:
            self._done.append(Element(tag, pairs, ""))
        else:
            self._open.append((tag, pairs, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._done.append(
            Element(tag, {name: value or "" for name, value in attrs}, ""),
        )

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                self._close_from(index)
                return

    def handle_data(self, data: str) -> None:
        for _, _, chunks in self._open:
            chunks.append(data)

    def _close_from(self, index: int) -> None:
        for tag, attrs, chunks in reversed(self._open[index:]):
            self._done.append(Element(tag, attrs, "".join(chunks)))
        del self._open[index:]

    def elements(self) -> list[Element]:
        """Return everything parsed, closing whatever the document left open."""
        self._close_from(0)
        return list(self._done)


def _elements_of(html: str, origin: str) -> list[Element]:
    """Return every element in `html`, refusing a parse that proves nothing.

    The non-vacuity gate lives here, in the only way in, so no rule below can hold
    over an empty list -- which is what a parse of an error page, a redirect body or
    an empty string would hand back, quietly and in every assertion at once.
    """
    parser = _Collector()
    parser.feed(html)
    parser.close()
    elements = parser.elements()

    landmarks = [
        element
        for element in elements
        if element.tag == "main" and element.attrs.get("id") == "conteudo"
    ]
    assert len(landmarks) == 1, (
        f"{origin}: parsed {len(elements)} elements and found {len(landmarks)} "
        f'<main id="conteudo"> landmarks, expected exactly one. The parse did not '
        f"reach templates/{BASE_TEMPLATE}, so every rule below it would hold over "
        f"nothing"
    )
    return elements


def _base_elements() -> list[Element]:
    """Return every element `templates/base.html` renders on its own."""
    return _elements_of(render_to_string(BASE_TEMPLATE), f"templates/{BASE_TEMPLATE}")


def _base_source() -> str:
    """Return the layout's template source, refusing a file that is not the layout."""
    assert BASE_SOURCE.is_file(), f"{BASE_SOURCE} does not exist"
    text = BASE_SOURCE.read_text(encoding="utf-8")
    assert "{% block content %}" in text, (
        f"{BASE_SOURCE}: no content block, so this is not the layout -- a source "
        f"scan over the wrong file matches nothing and reports a pass"
    )
    return text


def _app_js_source() -> str:
    """Return the first-party script, refusing a file that proves nothing.

    Both gates are here rather than in a test of their own. A missing file and an
    empty file each satisfy every forbidden-construct scan below, and `pytest -k`
    would deselect the one test that noticed.
    """
    assert APP_JS.is_file(), (
        f"{APP_JS} does not exist: it is the only first-party script "
        f"templates/{BASE_TEMPLATE} loads, and a missing file contains no `eval`, "
        f"no `new Function` and no `hx-on:` either"
    )
    source = APP_JS.read_text(encoding="utf-8")
    assert source.strip(), (
        f"{APP_JS} is empty, which is not the same as being safe -- every scan "
        f"below it would pass over nothing"
    )
    return source


def _queue_success(client: Client, text: str) -> None:
    """Set a success message on `client`, through the public API.

    `messages.success` needs a request and the test client hands out none, so a
    throwaway request is bound to the client's own session and cookies, and the
    storage is flushed back onto both. Writing the `_messages` key by hand instead
    would assert against this test's idea of the wire format rather than Django's.

    The configured storage is used rather than a chosen one because the default is
    `FallbackStorage`, and it stops reading at the first backend that says it
    returned everything. A message written straight into the session is therefore
    invisible whenever a cookie already holds one -- which it does here, allauth
    having set "Conectado com sucesso" during sign-in.
    """
    request: Any = RequestFactory().get("/")
    request.session = client.session
    request.COOKIES = {name: morsel.value for name, morsel in client.cookies.items()}
    request._messages = default_storage(request)
    messages.success(request, text)

    # Nothing is served from this response; it is the carrier the storage writes its
    # cookie onto, the way a real response would be.
    carrier = HttpResponse()
    request._messages.update(carrier)
    request.session.save()
    for name, morsel in carrier.cookies.items():
        client.cookies[name] = morsel.value


@pytest.fixture
def firm(settings: SettingsWrapper) -> Firm:
    """A firm on its own subdomain, which `TenantMiddleware` has to resolve."""
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]
    return make_firm("alpha-feedback")


@pytest.mark.django_db(transaction=True)
def test_a_success_message_is_announced_in_a_polite_status_region(firm: Firm) -> None:
    """A confirmation nobody is told about is a confirmation that did not happen.

    Driven through an existing firm-side route rather than a view written for the
    test, so what is asserted is the layout every page inherits.
    """
    client = firm.as_owner()
    _queue_success(client, MESSAGE)

    response = client.get(reverse("team"))
    assert response.status_code == HTTPStatus.OK

    # Proven before anything is asserted about the markup: the message really did
    # reach this request. Without it, a layout that renders messages perfectly and a
    # fixture that never set one are the same failure.
    queued = [str(message) for message in response.context["messages"]]
    assert MESSAGE in queued, (
        f"the success message never reached the request, so nothing below tests "
        f"the layout; messages on the response were {queued}"
    )

    elements = _elements_of(response.content.decode(), "the rendered team page")
    containers = [element for element in elements if MESSAGE in element.text]
    assert containers, (
        f"templates/{BASE_TEMPLATE} rendered no element containing {MESSAGE!r}: "
        f"the layout never draws django.contrib.messages, so every success a view "
        f"sets is discarded on the next render"
    )

    announced = [
        element
        for element in containers
        if element.attrs.get("role") == "status"
        and element.attrs.get("aria-live") == "polite"
    ]
    found = [
        (element.tag, element.attrs.get("role"), element.attrs.get("aria-live"))
        for element in containers
    ]
    assert announced, (
        f"templates/{BASE_TEMPLATE}: {MESSAGE!r} is on the page, but no element "
        f'containing it carries both role="status" and aria-live="polite", so a '
        f"screen reader never announces it. Containers as (tag, role, aria-live): "
        f"{found}"
    )


def test_the_layout_carries_exactly_one_assertive_live_region() -> None:
    """Two assertive regions race, and the interruption is the point of having one."""
    assertive = [
        element
        for element in _base_elements()
        if element.attrs.get("aria-live") == "assertive"
    ]
    described = [
        f"<{element.tag} id={element.attrs.get('id')!r}>" for element in assertive
    ]
    assert len(assertive) == 1, (
        f"templates/{BASE_TEMPLATE} renders {len(assertive)} elements with "
        f'aria-live="assertive", expected exactly one so HTMX announcements cannot '
        f"race each other or interrupt twice; found {described}"
    )
    assert assertive[0].attrs.get("id") == ANNOUNCE_ID, (
        f"templates/{BASE_TEMPLATE}: the assertive live region is "
        f"{described[0]}, expected id={ANNOUNCE_ID!r} -- static/js/app.js writes "
        f"into it by id and has nothing to find otherwise"
    )


def test_the_assertive_region_is_reachable_under_one_stable_id() -> None:
    """Counted from the other side: one `#htmx-announce`, and it is the assertive one.

    Two elements sharing the id satisfies the count above while `getElementById`
    reaches only the first of them, which may be the one without `aria-live`.
    """
    named = [
        element
        for element in _base_elements()
        if element.attrs.get("id") == ANNOUNCE_ID
    ]
    described = [
        f"<{element.tag} aria-live={element.attrs.get('aria-live')!r}>"
        for element in named
    ]
    assert len(named) == 1, (
        f"templates/{BASE_TEMPLATE} renders {len(named)} elements with "
        f'id="{ANNOUNCE_ID}", expected exactly one; found {described}'
    )
    assert named[0].attrs.get("aria-live") == "assertive", (
        f"templates/{BASE_TEMPLATE}: #{ANNOUNCE_ID} is {described[0]}, expected "
        f'aria-live="assertive" -- a region that is not live announces nothing'
    )


def test_the_layout_loads_app_js_from_this_origin_with_defer() -> None:
    """One deferred, same-origin `<script src>`. The policy allows nothing else."""
    scripts = [element for element in _base_elements() if element.tag == "script"]
    sources = [element.attrs.get("src", "") for element in scripts]
    loaded = [source for source in sources if source.endswith(APP_JS_LOGICAL)]
    assert len(loaded) == 1, (
        f"templates/{BASE_TEMPLATE} loads {APP_JS_LOGICAL} {len(loaded)} times, "
        f"expected exactly one <script src>; the scripts it loads are {sources}"
    )

    source = loaded[0]
    assert EXTERNAL_URL.match(source) is None, (
        f'templates/{BASE_TEMPLATE}: <script src="{source}"> names an external '
        f"origin -- script-src is 'self', so the browser refuses it and every "
        f"behaviour built on this file is simply absent in production"
    )
    assert source.startswith(STATIC_URL), (
        f'templates/{BASE_TEMPLATE}: <script src="{source}"> does not resolve '
        f"under {STATIC_URL}"
    )

    tag = next(element for element in scripts if element.attrs.get("src") == source)
    assert "defer" in tag.attrs, (
        f'templates/{BASE_TEMPLATE}: <script src="{source}"> is not deferred, so '
        f"it runs before the elements it binds to exist and blocks the parser "
        f"while it downloads"
    )


def test_the_layout_references_app_js_through_the_static_tag() -> None:
    """A hand-written path renders identically today and breaks under hashing.

    `collectstatic` runs through the manifest storage in production, so the file on
    disk is `app.<digest>.js`. `{% static %}` is what looks that name up; a literal
    `/static/js/app.js` in the template resolves to a file that is not there.
    """
    assert STATIC_TAG.search(_base_source()) is not None, (
        f"{BASE_SOURCE}: {APP_JS_LOGICAL} is not referenced through "
        f"{{% static %}} -- write the tag rather than the path, and never an "
        f"absolute or scheme-relative URL"
    )


def test_app_js_names_no_construct_the_content_security_policy_forbids() -> None:
    """The whole policy rests on the first-party script needing no relaxation.

    `script-src 'self'` with no `'unsafe-eval'` refuses `eval` and `new Function`
    outright. `hx-on:` is the one HTMX feature that compiles a string with
    `new Function`, and `allowEval: false` in the htmx-config meta disables it -- so
    a handler written here fails silently rather than visibly.
    """
    source = _app_js_source()
    offenders = [
        f"{APP_JS.name}:{number}: {label}: {line.strip()}"
        for label, pattern in FORBIDDEN_JS
        for number, line in enumerate(source.splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not offenders, (
        f"{APP_JS} uses a construct this project's Content-Security-Policy "
        f"forbids; keeping it would require widening script-src, which "
        f"apps/security/csp.py refuses to build: {offenders}"
    )

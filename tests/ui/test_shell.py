"""The firm shell's navigation disclosure — native markup, no script required.

The bar in `templates/base.html` is the only way into every firm-side screen, so it
has to survive the two conditions a script cannot be relied on to hold: a bundle that
never loaded, and a viewport too narrow to lay seven links out in a row. A `<details>`
element answers both without JavaScript, because the browser itself owns the open and
closed states and announces them as an expandable group.

That is the whole point of the assertions below, and it is why they are written
against attribute *names* rather than rendered appearance: a disclosure carrying an
`x-` or `hx-` attribute looks identical in a screenshot and is broken the moment the
bundle 404s. Every helper here gates on the disclosure existing before it measures
anything about it, so none of these can pass by finding nothing to check.
"""

import re
from http import HTTPStatus

import pytest
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import DjangoAssertNumQueries, SettingsWrapper

from apps.core.navigation import visible_nav_items
from apps.core.tenancy import tenant_context
from tests.ui.factories import Firm, make_firm
from tests.ui.test_navigation import NAV_QUERY_BUDGET

pytestmark = pytest.mark.django_db(transaction=True)

SOURCE = "templates/base.html"
DISCLOSURE_CLASS = "nav-disclosure"
SUMMARY_LABEL = "Menu"
CLOSE_DETAILS = "</details>"
CLOSE_SUMMARY = "</summary>"

DETAILS_OPEN = re.compile(r"<details\b[^>]*>", re.IGNORECASE)
SUMMARY_OPEN = re.compile(r"<summary\b[^>]*>", re.IGNORECASE)
ANY_TAG = re.compile(r"<[^>]*>")

# An attribute value, quoted either way. Substituted out of an open tag before its
# attribute *names* are read, so that a value such as class="hx-like-thing" is never
# counted as an attribute called `hx-like-thing`.
QUOTED_VALUE = re.compile(r"""=\s*(["']).*?\1""", re.DOTALL)
TAG_HEAD = re.compile(r"<\s*[a-zA-Z][\w-]*")
ATTRIBUTE_NAME = re.compile(r"[a-zA-Z_@:][a-zA-Z0-9_:.@-]*")

# The two prefixes that would make the fallback depend on a bundle. `x-cloak` carries
# no value at all, which is why names are read positionally rather than as pairs.
SCRIPTED_PREFIXES = ("x-", "hx-")


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("alpha-shell")


def _firm_page(firm: Firm) -> str:
    """Render a firm-side screen that is itself a nav destination.

    `team` is chosen because it is in `NAV`, so the page it returns is the one where
    the active-item marker has to appear.
    """
    response = firm.as_owner().get(reverse("team"))
    assert response.status_code == HTTPStatus.OK, (
        f"the Equipe page returned {response.status_code}; "
        "nothing below is measuring the firm shell"
    )
    return response.content.decode()


def _public_page(firm: Firm) -> str:
    """Render an anonymous page on the firm's own subdomain."""
    response = Client(SERVER_NAME=firm.host).get(reverse("dsr-submit"))
    assert response.status_code == HTTPStatus.OK, (
        f"the public DSR page returned {response.status_code}"
    )
    return response.content.decode()


def _attribute_names(open_tag: str) -> list[str]:
    """Return every attribute name written on an open tag, valueless ones included."""
    head = TAG_HEAD.match(open_tag)
    assert head is not None, f"{open_tag!r} is not an open tag"
    body = open_tag[head.end() : -1].rstrip("/")
    return [
        name.lower() for name in ATTRIBUTE_NAME.findall(QUOTED_VALUE.sub("=", body))
    ]


def _class_tokens(open_tag: str) -> set[str]:
    match = re.search(r"""class\s*=\s*(["'])(.*?)\1""", open_tag, re.DOTALL)
    return set(match.group(2).split()) if match else set()


def _text_of(fragment: str) -> str:
    """Return a fragment's visible text, tags and whitespace collapsed away."""
    return " ".join(ANY_TAG.sub(" ", fragment).split())


def _nav_disclosure(body: str, *, where: str) -> str:
    """Return the `<details class="nav-disclosure">` region, gate included.

    The gate lives here rather than in a test of its own so that every caller is
    non-vacuous by construction: no assertion downstream can be satisfied by a page
    that simply has no disclosure on it.
    """
    opened = [
        match
        for match in DETAILS_OPEN.finditer(body)
        if DISCLOSURE_CLASS in _class_tokens(match.group(0))
    ]
    assert opened, (
        f'{where}: {SOURCE} emits no <details class="{DISCLOSURE_CLASS}"> — '
        "the firm nav has no native, script-free disclosure"
    )
    assert len(opened) == 1, (
        f'{where}: {SOURCE} emits {len(opened)} <details class="{DISCLOSURE_CLASS}"> '
        "elements; the nav is one control, not several"
    )
    end = body.find(CLOSE_DETAILS, opened[0].end())
    assert end != -1, (
        f'{where}: the <details class="{DISCLOSURE_CLASS}"> opened by {SOURCE} '
        f"is never closed with {CLOSE_DETAILS}"
    )
    return body[opened[0].start() : end + len(CLOSE_DETAILS)]


def _summary_of(region: str, *, where: str) -> tuple[str, str]:
    """Return the disclosure's `<summary>` open tag and its accessible name."""
    opened = SUMMARY_OPEN.search(region)
    assert opened is not None, (
        f'{where}: the <details class="{DISCLOSURE_CLASS}"> in {SOURCE} has no '
        "<summary>; a disclosure without one has no label and no keyboard affordance"
    )
    end = region.find(CLOSE_SUMMARY, opened.end())
    assert end != -1, (
        f"{where}: the <summary> in {SOURCE} is never closed with {CLOSE_SUMMARY}"
    )
    text = _text_of(region[opened.end() : end])
    if text:
        return opened.group(0), text
    labelled = re.search(
        r"""aria-label\s*=\s*(["'])(.*?)\1""",
        opened.group(0),
        re.DOTALL,
    )
    return opened.group(0), labelled.group(2).strip() if labelled else ""


def test_the_firm_nav_offers_a_native_disclosure_labelled_menu(firm: Firm) -> None:
    """One `<details>`, one `<summary>`, and a name a person can read out loud."""
    region = _nav_disclosure(_firm_page(firm), where="the owner's Equipe page")
    _open_tag, name = _summary_of(region, where="the owner's Equipe page")
    assert name == SUMMARY_LABEL, (
        f'the <summary> of <details class="{DISCLOSURE_CLASS}"> in {SOURCE} is '
        f"labelled {name!r}, not {SUMMARY_LABEL!r}"
    )


def test_the_disclosure_is_browser_behaviour_not_alpine_or_htmx(firm: Firm) -> None:
    """An `x-` or `hx-` attribute here makes the fallback need the thing it backs up."""
    where = "the owner's Equipe page"
    region = _nav_disclosure(_firm_page(firm), where=where)
    summary_open, _name = _summary_of(region, where=where)
    details_open = DETAILS_OPEN.match(region)
    assert details_open is not None, f"{where}: the disclosure region lost its open tag"

    for element, open_tag in (
        ("<details>", details_open.group(0)),
        ("<summary>", summary_open),
    ):
        scripted = [
            name
            for name in _attribute_names(open_tag)
            if name.startswith(SCRIPTED_PREFIXES)
        ]
        assert scripted == [], (
            f"the {element} of the nav disclosure in {SOURCE} carries {scripted}; "
            "the open and closed states must be the browser's, not a bundle's"
        )


def test_the_disclosure_holds_the_nav_with_the_active_item_marked(firm: Firm) -> None:
    """With no script the disclosure *is* the bar, so the marker has to live in it."""
    where = "the owner's Equipe page"
    region = _nav_disclosure(_firm_page(firm), where=where)
    assert "<nav" in region, (
        f'the <details class="{DISCLOSURE_CLASS}"> in {SOURCE} contains no <nav> '
        "landmark; with the bundle absent it would offer no destinations at all"
    )
    assert 'aria-current="page"' in region, (
        f'the <nav> inside <details class="{DISCLOSURE_CLASS}"> in {SOURCE} marks no '
        "active item; the Equipe page is itself a nav destination"
    )


def test_an_anonymous_visitor_gets_neither_a_nav_nor_the_disclosure(
    firm: Firm,
) -> None:
    """The disclosure belongs inside the firm guard, exactly where the nav already is.

    Gated on the signed-in page first: without that, a shell which had simply dropped
    the feature would pass this twice over.
    """
    _nav_disclosure(_firm_page(firm), where="the owner's Equipe page")
    body = _public_page(firm)
    assert "<nav" not in body, (
        f"{SOURCE} draws a <nav> landmark for an anonymous visitor on "
        f"{firm.host}; an empty bar is noise to announce"
    )
    assert DISCLOSURE_CLASS not in body, (
        f'{SOURCE} draws <details class="{DISCLOSURE_CLASS}"> for an anonymous '
        f"visitor on {firm.host}; it belongs inside the "
        "`request.tenant and request.user.is_authenticated` guard"
    )


def test_the_disclosure_does_not_raise_the_nav_query_budget(
    firm: Firm,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    """Rendering the bar twice — once flat, once in the disclosure — would double it."""
    _nav_disclosure(_firm_page(firm), where="the owner's Equipe page")
    assert NAV_QUERY_BUDGET == 3, (
        "tests/ui/test_navigation.py pinned the bar at 3 queries; the disclosure is "
        f"markup and may not move it to {NAV_QUERY_BUDGET}"
    )
    with tenant_context(firm.tenant.id), django_assert_num_queries(NAV_QUERY_BUDGET):
        visible_nav_items(firm.owner)


def test_the_shell_still_renders_with_no_request_and_no_context(firm: Firm) -> None:
    """`base.html` is rendered bare by the asset tests; the disclosure must not break
    that.

    A `<details>` reaching for `request` outside the guard raises here long before it
    reaches a browser.
    """
    _nav_disclosure(_firm_page(firm), where="the owner's Equipe page")
    html = render_to_string("base.html")
    assert "<main" in html, f"{SOURCE} renders no <main> with no request or context"
    assert DISCLOSURE_CLASS not in html, (
        f'{SOURCE} emits <details class="{DISCLOSURE_CLASS}"> with no request at all; '
        "the firm guard cannot have been consulted"
    )

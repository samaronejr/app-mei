"""The portal's own accessible shell: landmarks, skip link, one heading, one bar.

A mirror of `tests/ui/test_layout.py`, rendered through the portal host as a
client-role account. Mirrored rather than shared on purpose. The two layouts are
separate files by design — `apps/portal/templates/portal/base.html` extends nothing,
because the firm shell calls `{% nav_items %}` unconditionally and `app_portal` holds
SELECT on none of the tables that reaches — and that separation is exactly why the
promises have to be pinned twice. A test that only ever renders the firm layout says
nothing about the portal, and every accessibility guarantee the firm side already
holds would have to be rediscovered here by a user rather than by a runner.

Four pages are walked rather than one. The shell's contracts are claims about *every*
portal screen, and the three destinations registered alongside it are the ones a later
wave replaces in place — so the walk is what stops todo 24, 25 or 26 from quietly
dropping a landmark on the page it rewrites.
"""

import re
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST: Final = "acme-portal.localhost"
PORTAL_URLCONF: Final = "apps.portal.urls"
PASSWORD: Final = "sufficiently-long-passphrase"  # noqa: S105

PORTAL_TEMPLATES: Final[Path] = (
    Path(__file__).resolve().parents[2] / "apps" / "portal" / "templates" / "portal"
)

SHELL: Final = "portal/base.html"
NAV_PARTIAL: Final[Path] = PORTAL_TEMPLATES / "partials" / "nav.html"

# Every top-level destination the portal offers, in the order the bar draws them. The
# count is the contract: four stable places is what makes a persistent bar legible
# rather than a menu that happens to be pinned to an edge.
NAV_URL_NAMES: Final = (
    "portal-home",
    "portal-payments",
    "portal-documents",
    "portal-account",
)
NAV_SIZE: Final = len(NAV_URL_NAMES)

# Every page the shell is walked across. Identical to the bar's destinations today and
# asserted to stay so, because a bar advertising a fifth page nobody tests, or a page
# the bar cannot reach, are both ways for this to drift.
PORTAL_PAGES: Final = NAV_URL_NAMES

H1: Final = re.compile(r"<h1[\s>]", re.IGNORECASE)
LANDMARKS: Final = ("<header", "<nav", "<main", "<footer")

# The rendered accessible name. `locale/` ships no compiled catalogue, so
# `{% translate 'Navegação principal' %}` emits its own msgid and this is literally the
# byte string the document carries.
NAV_LABEL: Final = "Navegação principal"

NAV_OPEN: Final = re.compile(
    rf'<nav\b[^>]*aria-label="{NAV_LABEL}"[^>]*>',
    re.IGNORECASE,
)
CLOSE_NAV: Final = "</nav>"

ANCHOR: Final = re.compile(r'<a\b[^>]*href="(?P<href>[^"]*)"[^>]*>', re.IGNORECASE)
BUTTON: Final = re.compile(r"<button\b[^>]*>", re.IGNORECASE)
CURRENT: Final = re.compile(r'aria-current="page"', re.IGNORECASE)

# `{% url 'name' %}`, either quote style. Read off the partial's source rather than a
# rendered page: a name that fails to reverse raises during rendering, so a scan of the
# document could only ever see the names that already worked.
URL_TAG: Final = re.compile(r"""\{%\s*url\s+(["'])(?P<name>[\w-]+)\1""")

# Any script element at all. The portal ships none — see the assertion below.
SCRIPT: Final = re.compile(r"<script\b", re.IGNORECASE)

STYLESHEET: Final = "css/app.css"


@pytest.fixture(autouse=True)
def _portal_urls(settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]
    # Pinned to the shipped tree. Sibling modules in this package point the middleware
    # at `tests.portal.urls`, which mounts probes instead of pages.
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", PORTAL_URLCONF)


@pytest.fixture
def portal_client() -> Client:
    """A signed-in client-role account, enrolled, on their firm's portal host."""
    tenant = Tenant.objects.create(name="Acme", slug="acme")
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding, before any portal context exists.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="CLIENTE ACME",
            cnpj="11222333000181",
            is_mei=True,
        )
    user: User = User.objects.create_user(email="mei@acme.example", password=PASSWORD)
    enrol_totp(user)
    Membership.objects.create(
        user=user,
        tenant=tenant,
        role=TenantRole.CLIENT_OWNER,
        client=company,
    )
    http = Client()
    http.force_login(user)
    return http


def _path(name: str) -> str:
    """Reverse a portal destination against the tree the portal host actually serves.

    `reverse()` with no urlconf resolves against `ROOT_URLCONF`, which mounts none of
    these names — so an unqualified call here raises `NoReverseMatch` regardless of
    whether the portal routes exist.
    """
    return str(reverse(name, urlconf=PORTAL_URLCONF))


def _page(http: Client, name: str) -> str:
    """GET one portal page and return the rendered document.

    The status gate is the non-vacuity control for every scan below. A portal template
    reaching a table `app_portal` cannot read raises `ProgrammingError` rather than
    returning a document, and a redirect returns an empty body that satisfies "this
    page draws no script" perfectly.
    """
    response = http.get(_path(name), headers={"host": PORTAL_HOST})
    assert response.status_code == HTTPStatus.OK, (
        f"{name} answered {response.status_code} on {PORTAL_HOST}, so nothing scanned "
        f"below is a rendered page"
    )
    return str(response.content.decode())


def _nav_of(body: str, *, where: str) -> str:
    """Return the navigation landmark, gate included."""
    opened = NAV_OPEN.search(body)
    assert opened is not None, (
        f'{where}: no <nav aria-label="{NAV_LABEL}"> in the document, so every '
        f"assertion about what the bar offers would quantify over an empty string"
    )
    end = body.find(CLOSE_NAV, opened.end())
    assert end != -1, f"{where}: the bar's <nav> is never closed with {CLOSE_NAV}"
    return body[opened.start() : end + len(CLOSE_NAV)]


def _portal_templates() -> list[Path]:
    found = sorted(PORTAL_TEMPLATES.rglob("*.html"))
    assert found, f"no templates under {PORTAL_TEMPLATES}"
    return found


def test_the_portal_document_declares_brazilian_portuguese(
    portal_client: Client,
) -> None:
    assert '<html lang="pt-br">' in _page(portal_client, "portal-home")


def test_the_portal_shell_links_the_compiled_stylesheet(portal_client: Client) -> None:
    """The same sheet the firm side ships, which is why the portal can stay query-free.

    A stylesheet is a static asset: no context processor, no model, no table. Reaching
    for a second, portal-only sheet would double the surface `npm run build` has to
    keep in step for no design the firm side does not already carry.
    """
    expected = f'<link rel="stylesheet" href="{settings.STATIC_URL}{STYLESHEET}">'
    for name in PORTAL_PAGES:
        assert expected in _page(portal_client, name), name


def test_the_portal_ships_no_javascript(portal_client: Client) -> None:
    """Zero bundles, on the one boundary in this product that RLS is enforcing.

    Pinned on the rendered document rather than on `portal/base.html` alone, so a
    child template that grows its own tag is caught by the same case.
    """
    for name in PORTAL_PAGES:
        body = _page(portal_client, name)
        assert SCRIPT.search(body) is None, (
            f"{name} loads a script; the portal renders plain server-side HTML so that "
            f"a shared bundle is never a shared attack surface across this boundary"
        )


def test_every_portal_page_carries_exactly_one_h1(portal_client: Client) -> None:
    """Two <h1> elements make a screen reader's document outline ambiguous."""
    for name in PORTAL_PAGES:
        body = _page(portal_client, name)
        assert len(H1.findall(body)) == 1, (
            f"{name} renders {len(H1.findall(body))} h1 elements; the layout owns "
            f"exactly one and each page fills it through the heading block"
        )


def test_no_portal_template_but_the_shell_emits_an_h1() -> None:
    """The rule above holds structurally only while the shell owns the tag."""
    offenders = [
        path.name
        for path in _portal_templates()
        if H1.search(path.read_text(encoding="utf-8")) and path.name != "base.html"
    ]
    assert offenders == [], (
        f"{offenders} write their own h1; fill {SHELL}'s heading block instead, or "
        f"'exactly one per page' becomes a rule every future page has to remember"
    )


def test_the_skip_link_is_the_first_focusable_element_and_has_a_target(
    portal_client: Client,
) -> None:
    body = _page(portal_client, "portal-home")
    skip = re.search(r'<a class="skip-link" href="#([\w-]+)"', body)
    assert skip is not None, "no skip link on the portal shell"
    target = skip.group(1)

    # It must precede the header, or it is not the first thing a keyboard reaches.
    assert body.index("skip-link") < body.index("<header")
    # …and it must land on something focusable, or focus stays in the header — one
    # keystroke instead of the whole bar, on every page.
    assert f'<main id="{target}" tabindex="-1"' in body


def test_every_portal_page_exposes_the_four_landmarks(portal_client: Client) -> None:
    for name in PORTAL_PAGES:
        body = _page(portal_client, name)
        for landmark in LANDMARKS:
            assert landmark in body, f"{name} draws no {landmark}"


def test_the_focus_ring_is_defined_and_never_removed(settings: SettingsWrapper) -> None:
    """`outline: none` with no replacement is a WCAG 2.4.7 failure.

    The portal has no stylesheet of its own, so this reads the same compiled file the
    firm-side case does. Duplicated deliberately: the assertion that matters is that
    the sheet the *portal* links carries a ring, and the two would stop being the same
    file the moment anyone gave the portal its own.
    """
    stylesheet = (settings.BASE_DIR / "static" / "css" / "app.css").read_text()
    compact = stylesheet.replace(" ", "")
    assert ":focus-visible" in compact
    assert "outline:2pxsolid" in compact


def test_the_bar_offers_exactly_the_four_top_level_destinations(
    portal_client: Client,
) -> None:
    """Four links, each to a page that exists, and nothing else in the landmark."""
    nav = _nav_of(_page(portal_client, "portal-home"), where="the portal home")
    hrefs = [match.group("href") for match in ANCHOR.finditer(nav)]

    assert len(hrefs) == NAV_SIZE, (
        f"the bar draws {len(hrefs)} links rather than {NAV_SIZE}: {hrefs}. A "
        f"persistent bar is legible because its contents are the same on every screen"
    )
    assert hrefs == [_path(name) for name in NAV_URL_NAMES], (
        f"the bar's destinations are {hrefs}, not the four top-level portal pages"
    )


def test_the_bar_is_the_same_on_every_page(portal_client: Client) -> None:
    """Static, and therefore free: no capability resolved, no row counted, no query.

    The destinations are identical everywhere because every one of them is FULL for
    both client roles by construction. A bar that varied would need a capability check
    at render time, inside the transaction and under the portal role, against tables
    that role cannot read.
    """
    for name in PORTAL_PAGES:
        nav = _nav_of(_page(portal_client, name), where=name)
        hrefs = [match.group("href") for match in ANCHOR.finditer(nav)]
        assert hrefs == [_path(url_name) for url_name in NAV_URL_NAMES], name


@pytest.mark.parametrize("name", PORTAL_PAGES)
def test_the_bar_marks_the_page_being_viewed(portal_client: Client, name: str) -> None:
    """`aria-current="page"` on exactly one link, and it is the one you are standing on.

    Parametrised rather than asserted once, because a shell that hard-coded the marker
    onto the first item would satisfy a single-page check and be wrong on three.
    """
    nav = _nav_of(_page(portal_client, name), where=name)
    marked = [
        match.group("href")
        for match in ANCHOR.finditer(nav)
        if CURRENT.search(match.group(0))
    ]

    assert marked == [_path(name)], (
        f"on {name} the bar marks {marked} as the current page rather than "
        f"[{_path(name)!r}]; the marker is what tells a screen reader which of four "
        f"identical destinations the reader has already arrived at"
    )


# ------------------------------------------------------ WCAG 2.2 AA target size (AA)
#
# 2.75rem is 44 CSS pixels, and 44 is the number a thumb hits reliably. Success
# criterion 2.5.8 sets the floor at 24; this product holds itself to 44 because the
# portal is a phone in the hand of somebody who is not an accountant, standing up, on
# the day a payment is due — and the cost of missing is either a wrong destination or
# a form submitted by accident.
#
# The size is carried by two component classes, both defined in `assets/css/app.css`
# with `@apply min-h-11` and compiled into the sheet every portal page links. So the
# promise has two halves that fail independently: the class can stop being written on
# the control, and the rule behind the class can stop declaring a minimum. A template
# scan sees only the first, and a stylesheet scan only the second, so both are read
# here — the classes off a RENDERED page, and the minimum off the COMPILED sheet.
#
# No exact spelling is pinned. The declaration is read for the length it resolves to
# and compared against the floor as a number, so retuning the scale or switching
# Tailwind's calc form for a literal rem is not a failure. What may not change quietly
# is that the control is at least 44px tall.

TOUCH_TARGET_CLASSES: Final = ("bottom-nav-link", "btn-lg")
MINIMUM_TARGET_PX: Final = 44
CSS_PIXELS_PER_REM: Final = 16

STYLESHEET_SOURCE: Final[Path] = (
    Path(__file__).resolve().parents[2] / "static" / "css" / "app.css"
)

# `--spacing:.25rem`, the scale every `calc(var(--spacing) * N)` is measured against.
SPACING_SCALE: Final = re.compile(r"--spacing:\s*([\d.]+)rem")

# A `min-height` declaration in either shape the build can emit: Tailwind's spacing
# calc, or a literal length once resolved.
MIN_HEIGHT_CALC: Final = re.compile(
    r"min-height:\s*calc\(\s*var\(--spacing\)\s*\*\s*([\d.]+)\s*\)",
)
MIN_HEIGHT_REM: Final = re.compile(r"min-height:\s*([\d.]+)rem")
MIN_HEIGHT_PX: Final = re.compile(r"min-height:\s*([\d.]+)px")

# The one portal control carrying `.btn-lg`: the sign-out button on the account page.
LARGE_BUTTON_PAGE: Final = "portal-account"


def _stylesheet() -> str:
    """Return the compiled sheet, refusing one that is missing or empty."""
    assert STYLESHEET_SOURCE.is_file(), (
        f"the compiled stylesheet is not at {STYLESHEET_SOURCE.as_posix()}; every "
        f"size read below would be read off nothing"
    )
    text = STYLESHEET_SOURCE.read_text(encoding="utf-8")
    assert text.strip(), f"{STYLESHEET_SOURCE.as_posix()} is empty"
    return text


def _minimum_height_px(sheet: str, component: str) -> float:
    """Return the smallest height one component class guarantees, in CSS pixels.

    THE GATE IS THE LOOKUP ITSELF. A class whose rule is not in the sheet, or whose
    rule declares no minimum height at all, is exactly what a control that has quietly
    stopped being 44px looks like — and both would otherwise return a number this
    function had invented rather than read.
    """
    opening = sheet.find(f".{component}{{")
    assert opening != -1, (
        f"the compiled sheet declares no rule for .{component}, so the class written "
        f"on the control resolves to nothing and the target has whatever size its "
        f"contents happen to give it"
    )
    close = sheet.find("}", opening)
    assert close != -1, f".{component}'s rule is never closed in the compiled sheet"
    body = sheet[opening:close]

    scale = SPACING_SCALE.search(sheet)
    assert scale is not None, (
        "the compiled sheet declares no --spacing scale, so a calc written against it "
        "cannot be resolved to a length and this check would be guessing"
    )
    step_px = float(scale.group(1)) * CSS_PIXELS_PER_REM

    if (calc := MIN_HEIGHT_CALC.search(body)) is not None:
        return float(calc.group(1)) * step_px
    if (rem := MIN_HEIGHT_REM.search(body)) is not None:
        return float(rem.group(1)) * CSS_PIXELS_PER_REM
    if (px := MIN_HEIGHT_PX.search(body)) is not None:
        return float(px.group(1))
    pytest.fail(
        f".{component} declares no min-height at all: {body}. The class is still "
        f"written on the control, so nothing else in this suite notices that the "
        f"target has stopped being guaranteed a size",
    )


@pytest.mark.parametrize("component", TOUCH_TARGET_CLASSES)
def test_each_touch_target_class_guarantees_a_thumb_sized_control(
    component: str,
) -> None:
    """The class means 44px, or the markup that carries it means nothing."""
    height = _minimum_height_px(_stylesheet(), component)
    assert height >= MINIMUM_TARGET_PX, (
        f".{component} guarantees {height:g}px of height and the floor on this "
        f"surface is {MINIMUM_TARGET_PX}px; a control below it is one a thumb misses, "
        f"and the two ways to miss here are opening the wrong page and submitting a "
        f"form nobody meant to submit"
    )


def test_every_bar_destination_is_drawn_as_a_touch_target(
    portal_client: Client,
) -> None:
    """All four, on all four pages — the bar is the same control set everywhere.

    Read off the rendered document rather than the partial, because the class only
    means anything on a page that actually drew it: a shell that stopped including the
    bar, or a page that rendered it through some other markup, both leave the partial
    on disk perfectly intact.
    """
    for name in PORTAL_PAGES:
        nav = _nav_of(_page(portal_client, name), where=name)
        drawn = [
            match.group(0)
            for match in ANCHOR.finditer(nav)
            if TOUCH_TARGET_CLASSES[0] in (match.group(0) or "")
        ]
        assert len(drawn) == NAV_SIZE, (
            f"{name}: {len(drawn)} of the bar's {NAV_SIZE} destinations carry "
            f".{TOUCH_TARGET_CLASSES[0]}, so the rest are as tall as the word inside "
            f"them and fall under the {MINIMUM_TARGET_PX}px floor: {drawn}"
        )


def test_the_account_page_draws_its_sign_out_as_a_large_target(
    portal_client: Client,
) -> None:
    """The one destructive control on this surface, and the one that must not be hit
    by accident nor missed by someone trying to hit it.

    Signing out is the only action on the portal a client cannot undo without their
    password and their second factor, which is precisely why it is drawn at the large
    size rather than the ordinary one.
    """
    body = _page(portal_client, LARGE_BUTTON_PAGE)
    large = [
        match.group(0)
        for match in BUTTON.finditer(body)
        if TOUCH_TARGET_CLASSES[1] in match.group(0)
    ]
    assert large, (
        f"{LARGE_BUTTON_PAGE} draws no button carrying .{TOUCH_TARGET_CLASSES[1]}, so "
        f"the sign-out control is rendered at the ordinary size and is no longer "
        f"guaranteed the {MINIMUM_TARGET_PX}px this surface holds itself to"
    )


def test_every_destination_the_bar_names_reverses_on_the_portal_tree() -> None:
    """`{% url %}` on a name the portal urlconf lacks is a 500 on every portal page.

    Read off the partial's source. The shell includes it unconditionally, so a missing
    name takes the whole portal down at once rather than one screen — and a scan of a
    rendered document cannot see it, because the render is what raises.

    Names are de-duplicated, first appearance winning, because the bar reverses each
    destination twice on purpose: once captured into a variable, which is what the
    active-item comparison needs and which fails *silently* on a missing name, and once
    written straight into the anchor, which is the spelling that raises. Asserting the
    raw sequence would pin that implementation detail rather than the destinations.
    """
    assert NAV_PARTIAL.is_file(), f"{NAV_PARTIAL} is not a shipped template"
    written = [
        match.group("name")
        for match in URL_TAG.finditer(NAV_PARTIAL.read_text(encoding="utf-8"))
    ]
    named = list(dict.fromkeys(written))

    assert named, (
        f"{NAV_PARTIAL.name} names no destination through the url tag, so the loop "
        f"below reverses nothing and this case passes for an empty bar"
    )
    assert named == list(NAV_URL_NAMES), (
        f"{NAV_PARTIAL.name} links {named} rather than the four top-level pages"
    )
    for name in named:
        # Raises NoReverseMatch here rather than mid-render on a page a client opened.
        _path(name)

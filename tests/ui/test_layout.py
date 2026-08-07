"""The accessible shell every page inherits: landmarks, skip link, single heading."""

import re
from http import HTTPStatus
from typing import Final

import pytest
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.authz.services import Actor, can
from apps.clients.models import ClientCompany
from apps.core.templatetags.ptbr import BRT
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, TenantRole
from tests.ui.factories import Firm, add_member, assign, make_firm
from tests.ui.templates_scan import loaded_templates

pytestmark = pytest.mark.django_db(transaction=True)

H1 = re.compile(r"<h1[\s>]", re.IGNORECASE)
LANDMARKS = ("<header", "<nav", "<main", "<footer")

# ------------------------------------------- the page header's slot, and the trail
#
# Two features of the shell, tested together because they are one contract seen from
# two sides. `{% block actions %}` puts a page's own controls beside its heading, and
# `partials/crumbs.html` puts a trail underneath it — and the trail emits a <nav>.
#
# That is the whole risk. `tests/ui/test_navigation.py:71-75` and
# `test_a_nav_landmark_is_omitted_rather_than_left_empty` above both pin the same
# promise: an anonymous visitor on a public page is shown ZERO nav landmarks. A slot
# in `base.html` that wrapped itself in a nav, a toolbar or a labelled region would
# break that on every public screen at once, and a trail included from a template a
# signed-out visitor can reach would break it on that one. So the slot is asserted to
# be structurally empty by default, and the trail is asserted to be included by
# exactly one template in the whole project.

SHELL: Final = "templates/base.html"
CRUMBS_FILE: Final = "crumbs.html"
CRUMBS_TEMPLATE: Final = "partials/crumbs.html"
TRAIL_SCREEN: Final = "clients/detail.html"

# The rendered accessible name. `locale/` ships no compiled catalogue, so
# `{% translate 'Trilha' %}` emits its own msgid and this is literally the byte string
# the document carries.
TRAIL_LABEL: Final = "Trilha"

# `{% include "partials/crumbs.html" %}`, either quote style. Matched against template
# *source* rather than a rendered page, because the claim is about every screen this
# project ships — including the ones no fixture here happens to render.
CRUMBS_INCLUDE: Final = re.compile(
    r"""\{%\s*include\s+(["'])partials/crumbs\.html\1""",
)

NAV_OPEN: Final = re.compile(r"<nav\b", re.IGNORECASE)
TRAIL_OPEN: Final = re.compile(
    rf'<nav\b[^>]*aria-label="{TRAIL_LABEL}"[^>]*>',
    re.IGNORECASE,
)
CLOSE_NAV: Final = "</nav>"

# Everything the layout emits between the heading and the end of the header row: the
# rendered `{% block actions %}`, and nothing else. A page that declares no actions
# leaves this whitespace, which is the property the "empty by default" case asserts.
HEADER_ROW: Final = re.compile(
    r"<h1\b[^>]*>.*?</h1>(?P<actions>.*?)</div>",
    re.DOTALL | re.IGNORECASE,
)

# The element carrying `aria-current="page"`, open tag only. Used to prove the last
# crumb is text: a link there is a control that does nothing when it is followed.
CURRENT_CRUMB: Final = re.compile(
    r'<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*aria-current="page"',
    re.IGNORECASE,
)

EXPORT_LABEL: Final = "Exportar clientes"
EXPORT_URL_NAME: Final = "clients-export-csv"
LIST_URL_NAME: Final = "clients-list"
DETAIL_URL_NAME: Final = "client-detail"
EXPORT_CAPABILITY: Final = "clients.view_all"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("alpha-layout")


def _public_page() -> str:
    return Client(SERVER_NAME="localhost").get(reverse("dsr-submit")).content.decode()


def test_the_document_declares_brazilian_portuguese() -> None:
    assert '<html lang="pt-br">' in _public_page()


def test_every_page_carries_exactly_one_h1(firm: Firm) -> None:
    """Two <h1> elements make a screen reader's document outline ambiguous."""
    pages = [_public_page()]
    signed_in = firm.as_owner()
    for name in ("team", "invite-issued"):
        response = signed_in.get(reverse(name))
        assert response.status_code == HTTPStatus.OK, name
        pages.append(response.content.decode())

    for body in pages:
        assert len(H1.findall(body)) == 1


def test_no_template_but_the_layout_emits_an_h1() -> None:
    """The rule above holds structurally only while base.html owns the tag."""
    offenders = [
        str(path)
        for path, text in loaded_templates()
        if H1.search(text) and path.name != "base.html"
    ]
    assert offenders == []


def test_the_skip_link_is_the_first_focusable_element_and_has_a_target() -> None:
    body = _public_page()
    skip = re.search(r'<a class="skip-link" href="#([\w-]+)"', body)
    assert skip is not None, "no skip link"
    target = skip.group(1)

    # It must precede the header, or it is not the first thing a keyboard reaches.
    assert body.index("skip-link") < body.index("<header")
    # …and it must land on something focusable, or focus stays in the header.
    assert f'<main id="{target}" tabindex="-1"' in body


def test_the_page_exposes_the_four_landmarks(firm: Firm) -> None:
    body = firm.as_owner().get(reverse("team")).content.decode()
    for landmark in LANDMARKS:
        assert landmark in body, landmark


def test_a_nav_landmark_is_omitted_rather_than_left_empty() -> None:
    """An anonymous page draws no nav, and an empty landmark is noise to announce."""
    body = _public_page()
    assert "<main" in body
    assert "<nav" not in body


def test_the_focus_ring_is_defined_and_never_removed(settings: SettingsWrapper) -> None:
    """`outline: none` with no replacement is a WCAG 2.4.7 failure."""
    stylesheet = (settings.BASE_DIR / "static" / "css" / "app.css").read_text()
    compact = stylesheet.replace(" ", "")
    assert ":focus-visible" in compact
    assert "outline:2pxsolid" in compact


def test_a_timestamp_renders_in_brasilia_time_on_a_real_page(firm: Firm) -> None:
    """The filters are unit-tested; this proves the layout actually applies them."""
    invite, _token = Invite.issue(
        tenant=firm.tenant,
        email="convidada@example.com",
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    body = firm.as_owner().get(reverse("team")).content.decode()
    expected = invite.expires_at.astimezone(BRT).strftime("%d/%m/%Y %H:%M")
    assert expected in body


@pytest.fixture
def carteira() -> Firm:
    """A firm with one client, assigned so that both roles can open its workspace."""
    firm = make_firm("alfa-trilha", client_count=1)
    assign(firm, firm.clients[0], firm.accountant)
    return firm


def _actions_slot(body: str, *, where: str) -> str:
    """Return whatever the layout rendered into the slot beside the heading.

    The gate lives here rather than in a test of its own, because every caller below
    reads this region and `pytest -k` can deselect a standalone sentinel. A document
    whose header row never matched would hand back the empty string, which satisfies
    "the export link is absent" for a page that failed to render at all.
    """
    row = HEADER_ROW.search(body)
    assert row is not None, (
        f"{where}: {SHELL} emitted no heading row — an h1 followed by the action slot "
        f"and closed off — so there is no slot to read and every assertion about what "
        f"is or is not offered beside the heading is a statement about nothing"
    )
    return row.group("actions")


def _trail_of(body: str, *, where: str) -> str:
    """Return the breadcrumb landmark, gate included."""
    opened = TRAIL_OPEN.search(body)
    assert opened is not None, (
        f'{where}: no <nav aria-label="{TRAIL_LABEL}"> in the document, so the trail '
        f"assertions below would quantify over an empty string"
    )
    end = body.find(CLOSE_NAV, opened.end())
    assert end != -1, f"{where}: the trail's <nav> is never closed with {CLOSE_NAV}"
    return body[opened.start() : end + len(CLOSE_NAV)]


def _detail_page(firm: Firm, company: ClientCompany) -> str:
    """GET one client's workspace as the owner and return the rendered document."""
    response = firm.as_owner().get(reverse(DETAIL_URL_NAME, args=[company.pk]))
    assert response.status_code == HTTPStatus.OK, (
        f"the client workspace answered {response.status_code} for "
        f"{company.legal_name}, so nothing scanned below is a rendered page"
    )
    return str(response.content.decode())


def _templates_including_the_trail() -> list[str]:
    """Return every loader-reachable template that includes the breadcrumb partial.

    Both gates are in here rather than in cases of their own, and both produce the
    same false pass: an empty list reads exactly like "the trail is confined", whether
    it came from a partial nobody ships or from a scan that reached no files.
    """
    scanned = list(loaded_templates())
    assert scanned, "the template scan reached no files at all"

    shipped = [path for path, _ in scanned if path.name == CRUMBS_FILE]
    assert len(shipped) == 1, (
        f"{CRUMBS_TEMPLATE} is not a shipped template ({len(shipped)} files named "
        f"{CRUMBS_FILE} found), so a scan for includes of it can only ever return the "
        f"empty list and the confinement claim below is vacuous"
    )
    return [path.as_posix() for path, text in scanned if CRUMBS_INCLUDE.search(text)]


def _rendered_registry(firm: Firm, user: Actor) -> str:
    """Render the registry template for one account, with no request behind it.

    Rendered rather than requested on purpose. The navigation bar carries its own copy
    of the export link, so a scan of a real response cannot tell the layout's action
    slot from the bar — and with no request the bar's `request.user.is_authenticated`
    guard is false, which leaves the slot as the only place the link could come from.
    The `{% can %}` tag still resolves against the seeded matrix, so the capability
    being asserted is the real one.
    """
    with tenant_context(firm.tenant.id):
        return str(render_to_string("clients/list.html", {"user": user}))


def test_the_action_slot_is_empty_and_landmark_free_by_default() -> None:
    """A page that declares no actions must add nothing at all to the document.

    Rendered bare, the way `tests/ui/test_asset_pipeline.py:77-93` renders it, so a
    slot reaching for `request` raises here rather than in a browser.
    """
    html = render_to_string("base.html")

    assert len(H1.findall(html)) == 1, (
        f"{SHELL} emits {len(H1.findall(html))} h1 elements once the heading shares a "
        f"row with the action slot; the layout owns exactly one"
    )
    assert NAV_OPEN.search(html) is None, (
        f"{SHELL} draws a <nav> with no request at all, so the action slot has been "
        f"given a landmark of its own and every public page now carries one"
    )
    assert _actions_slot(html, where="the bare shell").strip() == "", (
        f"{SHELL} renders something into the action slot by default; an empty slot "
        f"must emit no element, because a page that declares no actions has none"
    )


def test_the_registry_offers_its_export_from_the_action_slot(carteira: Firm) -> None:
    """The link moved beside the heading, and it is still a working destination."""
    export = reverse(EXPORT_URL_NAME)
    body = carteira.as_owner().get(reverse(LIST_URL_NAME)).content.decode()

    slot = _actions_slot(body, where="the owner's registry")
    assert f'href="{export}"' in slot, (
        f"the registry's heading row offers no link to {export}; the export action "
        f"belongs in the layout's slot beside the title it acts on"
    )
    assert EXPORT_LABEL in slot, (
        f"the link in the registry's action slot carries no readable label — "
        f"{EXPORT_LABEL!r} — so it is announced as an icon and nothing else"
    )


def test_the_action_slot_is_drawn_only_for_an_account_holding_the_capability(
    carteira: Firm,
) -> None:
    """The gate is `{% can %}`, so an account without it is offered nothing.

    The two accounts differ in exactly one thing that matters here: the published
    matrix gives `clients.view_all` to every firm-side role and to neither client-side
    one (`apps/authz/matrix.py:83-87`). The capability is asserted against the matrix
    first, so a future edit that grants it to the portal turns this red instead of
    quietly leaving the test measuring two accounts that both hold it.

    The owner's render is the non-vacuity gate. Without it, a template that had simply
    lost the block — or a render that raised into an empty string — would satisfy the
    absence assertion perfectly.
    """
    portal = add_member(
        carteira.tenant,
        f"portal@{carteira.tenant.slug}.example.com",
        TenantRole.CLIENT_OWNER,
        client=carteira.clients[0],
    )
    with tenant_context(carteira.tenant.id):
        assert can(carteira.owner, EXPORT_CAPABILITY), (
            f"the owner does not hold {EXPORT_CAPABILITY}, so the positive control "
            f"below is not the one this test is named for"
        )
        assert not can(portal, EXPORT_CAPABILITY), (
            f"{TenantRole.CLIENT_OWNER} now holds {EXPORT_CAPABILITY}, so the two "
            f"accounts no longer differ and the absence below proves nothing"
        )

    export = reverse(EXPORT_URL_NAME)

    granted = _rendered_registry(carteira, carteira.owner)
    assert f'href="{export}"' in _actions_slot(granted, where="the owner's registry"), (
        f"the owner holds {EXPORT_CAPABILITY} and is offered no link to {export}, so "
        f"the absence asserted below would hold for an account that has the "
        f"capability too"
    )

    refused = _rendered_registry(carteira, portal)
    assert _actions_slot(refused, where="a portal account's registry").strip() == "", (
        f"the action slot is drawn for an account without {EXPORT_CAPABILITY}; the "
        f"`can` gate has been dropped or weakened"
    )
    assert export not in refused, (
        f"{export} reaches the document of an account without {EXPORT_CAPABILITY}, so "
        f"the interface advertises an action the view answers 403 for"
    )


def test_the_client_workspace_renders_the_trail_as_an_ordered_list(
    carteira: Firm,
) -> None:
    """Clientes → razão social, in that order, with the parent an actual link."""
    company = carteira.clients[0]
    trail = _trail_of(_detail_page(carteira, company), where="the client workspace")

    assert "<ol" in trail, (
        "the trail is not an ordered list, so a screen reader announces two words "
        "rather than a position in a hierarchy"
    )
    assert f'href="{reverse(LIST_URL_NAME)}"' in trail, (
        f"the trail's parent crumb does not link to {reverse(LIST_URL_NAME)}; a "
        f"breadcrumb whose ancestor is not reachable is decoration"
    )
    assert "Clientes" in trail, "the parent crumb carries no label"
    assert company.legal_name in trail, (
        f"the trail does not name {company.legal_name}, the page it is drawn on"
    )


def test_the_current_crumb_is_marked_and_is_not_a_link(carteira: Firm) -> None:
    """`aria-current="page"` on text, never on an anchor to the page you are on."""
    company = carteira.clients[0]
    trail = _trail_of(_detail_page(carteira, company), where="the client workspace")

    marked = CURRENT_CRUMB.search(trail)
    assert marked is not None, (
        'the trail marks no crumb with aria-current="page", so nothing states which '
        "of its entries is the page the reader is standing on"
    )
    assert marked.group("tag").lower() != "a", (
        "the current crumb is an anchor; following it reloads the page the reader is "
        "already on, which is a control that does nothing"
    )


def test_the_trail_is_included_by_exactly_one_template(carteira: Firm) -> None:
    """One screen, and it is the one behind a sign-in.

    The rendered workspace is the gate: a scan proving "included once" says nothing if
    that one include never reaches a document.
    """
    _trail_of(
        _detail_page(carteira, carteira.clients[0]),
        where="the client workspace",
    )

    including = _templates_including_the_trail()
    assert len(including) == 1, (
        f"{CRUMBS_TEMPLATE} is included by {len(including)} templates — {including} — "
        f"and it emits a <nav>; every screen that grows one has to be checked against "
        f"the promise that a public page draws no nav landmark"
    )
    assert including[0].endswith(TRAIL_SCREEN), (
        f"{CRUMBS_TEMPLATE} is included by {including[0]} rather than {TRAIL_SCREEN}"
    )


def test_the_trail_landmark_never_reaches_an_anonymous_page(carteira: Firm) -> None:
    """The trail is a <nav>, and a public page's contract is no <nav> whatsoever.

    Gated on the signed-in workspace first, so a shell that had simply dropped the
    feature could not pass this by drawing nothing anywhere.
    """
    _trail_of(
        _detail_page(carteira, carteira.clients[0]),
        where="the client workspace",
    )

    body = Client(SERVER_NAME=carteira.host).get(reverse("dsr-submit")).content.decode()
    assert NAV_OPEN.search(body) is None, (
        f"a public page on {carteira.host} draws a <nav> landmark; the trail and the "
        f"bar both belong behind the sign-in guard"
    )
    assert TRAIL_LABEL not in body, (
        f"the trail's accessible name reaches an anonymous visitor on {carteira.host}"
    )

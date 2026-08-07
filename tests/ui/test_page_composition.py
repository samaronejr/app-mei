"""The six screens somebody meets cold, and the one shell rule that governs them all.

None of these pages is browsed to. Two are reached by a redirect, two by a refusal, one
by a link in a footer, and one by a middleware that has just taken an operator off
whatever they were doing. Nobody arrives with context, and nobody comes back to check
that the page still says anything — which is exactly how `invite_issued.html` spent a
wave rendering an empty `<main>` under a heading while every structural guard stayed
green. `tests/ui/test_confirmation_pages.py` covers the three that only announce; this
file covers the composition the rest of them gained, and it reads the page's own body
rather than the document, for the same reason that one does.

The nav contract is asserted here from BOTH sides, in one place, because it is the rule
this wave was most likely to break and it is a rule with two halves that pull opposite
ways. `tests/ui/test_navigation.py:71` and `tests/ui/test_layout.py:146` pin the first:
an anonymous visitor on a public page draws zero nav landmarks. `test_navigation.py:95`
pins the second: a SIGNED-IN accountant opening the very same public route still gets
the shell's bar, with the item their role is granted. A change that "fixed" the first by
suppressing the bar on public routes would satisfy every anonymous assertion in this
project and quietly take the navigation away from every signed-in person who follows the
LGPD link in the footer. So the two are written as one pair, against one URL, and the
anonymous leg is what proves the bar the accountant sees came from the shell rather than
from anything these pages added.

Every glyph assertion goes through `_glyph`, and the gate inside it is not decoration:
`"" in body` is True, so a partial that had been emptied would satisfy a bare substring
check on every page at once. It is also why each glyph is asserted beside the pt-BR
words it reinforces — an icon is never the only carrier of meaning here, and a test that
only looked for the shape could not tell the difference.
"""

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

from apps.accounts.models import User
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from tests.support import enrol_totp
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

MAIN: Final = re.compile(r"<main\b[^>]*>(.*?)</main>", re.DOTALL | re.IGNORECASE)

# The layout's last piece of furniture before `{% block content %}`. No page renders it,
# so its close is where a page's own body reliably begins.
MESSAGES: Final = re.compile(
    r"""<div\b[^>]*\bid=["']messages["'][^>]*>""",
    re.IGNORECASE,
)
DIV: Final = re.compile(r"<div\b|</div\s*>", re.IGNORECASE)
TAGS: Final = re.compile(r"<[^>]+>")

NAV_OPEN: Final = re.compile(r"<nav\b", re.IGNORECASE)

# The hidden control `{% csrf_token %}` renders. Asserted on the RESPONSE rather than in
# the source the way `tests/ui/test_form_contract.py` does, because a restructured form
# can lose the tag while the file still contains one somewhere else entirely.
CSRF_CONTROL: Final = 'name="csrfmiddlewaretoken"'

# The one nav item the published matrix grants a staff accountant. Spelled here rather
# than imported so this file states the fact it depends on instead of inheriting it.
SHARED_NAV_ITEM: Final = "Exportar clientes"

DEAD_TOKEN: Final = "esta-composicao-nunca-foi-emitida"  # noqa: S105
INVITED: Final = "convidada@composicao.example"
OPERATOR: Final = "operadora@composicao.example"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    """Both hosts: the selector and the invitation live on the platform one."""
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("alfa-composicao")


# ------------------------------------------------------------------- readers and gates


def _document(
    page: str,
    response: "_MonkeyPatchedWSGIResponse",
    expected: HTTPStatus,
) -> str:
    """Return the whole rendered document, refusing to return one nobody rendered.

    Both gates live here rather than in a case of their own: a redirect carries no body
    and a 403 from the wrong branch carries the wrong one, and either would satisfy
    "this page draws no nav landmark" perfectly.
    """
    assert response.status_code == expected, (
        f"{page}: answered {response.status_code}, wanted {expected} — every "
        f"assertion below would read the body of a different response"
    )
    html = response.content.decode()
    assert MAIN.search(html) is not None, (
        f"{page}: no <main> element in the response, so this is not a page the "
        f"layout rendered"
    )
    return html


def _body(
    page: str,
    response: "_MonkeyPatchedWSGIResponse",
    expected: HTTPStatus = HTTPStatus.OK,
) -> str:
    """Return what a page renders below the layout, refusing to return nothing.

    The strip matters more than it looks. The heading, the pre-rendered HTMX failure
    notice and the messages region are all writes `base.html` makes on every page, so
    "the main element is non-empty" is true of a template that overrides no content
    block at all — which is the exact state this project has already shipped once.
    """
    inner_match = MAIN.search(_document(page, response, expected))
    assert inner_match is not None
    inner = inner_match.group(1)

    opening = MESSAGES.search(inner)
    assert opening is not None, (
        f"{page}: the layout's messages region is missing from <main>, so where this "
        f"page's own body begins is unknown"
    )
    close = _matching_close(inner, opening.end())
    assert close is not None, (
        f"{page}: the layout's messages region is never closed, so every byte of the "
        f"layout beneath it would be read as this page's own body"
    )

    body = inner[close:]
    assert _text(body), (
        f"{page}: renders nothing of its own below the layout — it overrides no "
        f"content block, and its <main> is empty to a reader"
    )
    return body


def _matching_close(html: str, start: int) -> int | None:
    """Return the offset just past the `</div>` closing the div open at `start`."""
    depth = 1
    for token in DIV.finditer(html, start):
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            return token.end()
    return None


def _text(html: str) -> str:
    """Return the readable text of a fragment, with whitespace collapsed."""
    return " ".join(TAGS.sub(" ", html).split())


def _glyph(name: str) -> str:
    """Return one icon partial as it renders, refusing to return an empty needle.

    The gate is the whole point of routing every glyph assertion through here. An empty
    string is a substring of every document, so a partial that had been emptied — or a
    name misspelled into one the loader answers with nothing — would keep every icon
    assertion in this file green while no glyph reached any page at all.
    """
    svg = render_to_string(f"partials/pure/icons/{name}.html").strip()
    assert svg.startswith("<svg"), (
        f"partials/pure/icons/{name}.html did not render an <svg> root ({svg[:60]!r}); "
        f"searching a page for this would be searching it for nothing"
    )
    assert 'aria-hidden="true"' in svg, (
        f"partials/pure/icons/{name}.html is no longer decorative, so every page "
        f"below now announces a shape beside the words that already say it"
    )
    return svg


def _issued_invite(tenant: Tenant) -> str:
    """Issue a real invitation and return the raw token from its link."""
    _invite, raw_token = Invite.issue(
        tenant=tenant,
        email=INVITED,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    return str(raw_token)


def _operator_client(tenant: Tenant) -> Client:
    """A signed-in staff operator, enrolled the way the middleware requires."""
    operator = User.objects.create_user(email=OPERATOR, is_staff=True)
    Membership.objects.create(user=operator, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(operator)
    client = Client()
    client.force_login(operator)
    return client


# --------------------------------------------------- the admin selector, composed
#
# The one screen an operator does not choose to open: the middleware sends them here
# mid-task, and the first question is always which of two things went wrong — the
# company or the account. The page has to answer both without being read end to end.


def test_the_selector_frames_the_form_and_says_what_the_choice_records(
    firm: Firm,
) -> None:
    # Given a staff operator opening the selector
    body = _body(
        "admin/select_tenant.html",
        _operator_client(firm.tenant).get(reverse("admin-select-tenant")),
    )
    text = _text(body)

    # Then the form is framed as being about a company, glyph and words together
    assert _glyph("building-2") in body, (
        f"the selector's form carries no company mark: {text}"
    )
    assert "Empresa deste atendimento" in text, text
    assert "vínculo ativo" in text, (
        f"the page does not say the list is the operator's own firms: {text}"
    )

    # …and the consequence of choosing is stated where it is still a choice, not
    # afterwards in an audit table nobody on this screen can read
    assert _glyph("file-text") in body, text
    assert "O que fica registrado" in text, text
    assert "trilha de auditoria" in text, text
    assert "motivo escrito" in text, (
        f"break-glass access is offered with no statement that it needs a reason and "
        f"is recorded apart, which `authorize` and `record_selection` both enforce: "
        f"{text}"
    )

    # …and the form it wraps is still armed and still submits
    assert CSRF_CONTROL in body, f"the selector's form lost its CSRF control: {body}"
    assert "Selecionar" in text, text


def test_the_selector_refuses_a_firm_through_the_shared_notice(firm: Firm) -> None:
    """A refusal here is about the ACCOUNT, and it has to interrupt.

    Posted with a syntactically valid id that matches no row, so the form validates and
    `authorize` is what rejects it — the branch that fills the `error` context. A blank
    id would be caught by the form instead and would exercise the summary, which
    `tests/ui/test_form_contract.py` already pins.
    """
    # Given a selection the operator holds no membership for
    response = _operator_client(firm.tenant).post(
        reverse("admin-select-tenant"),
        {"tenant": str(uuid4()), "next": "/admin/"},
    )

    # When the refusal is read
    body = _body("admin/select_tenant.html", response, HTTPStatus.FORBIDDEN)
    text = _text(body)

    # Then it arrives through the shared error notice, which is what carries the role
    alert = re.search(r"<div\b[^>]*\bclass=[\"'][^\"']*\bnotice-error\b[^>]*>", body)
    assert alert is not None, f"no error notice on the refused selection: {text}"
    assert 'role="alert"' in alert.group(0), (
        f"the refusal is announced as a status rather than an alert, so a screen "
        f"reader reaches it whenever it gets round to it: {alert.group(0)}"
    )

    # …and it says what was refused as well as why. The reason `authorize` raises is
    # written for somebody who already knows what they clicked.
    assert "Empresa recusada." in text, text
    assert "No such firm" in text, (
        f"the reason handed to the template is no longer rendered: {text}"
    )

    # …and the form comes back rather than being replaced by the refusal
    assert CSRF_CONTROL in body, f"the refused selector offers no way to retry: {body}"


# ------------------------------------------------------ the rights request, composed


def test_the_rights_form_states_the_two_things_that_decide_whether_it_is_filled_in(
    firm: Firm,
) -> None:
    """No account is needed, and the answer goes to the address typed on the form."""
    # Given the public intake page
    body = _body(
        "lgpd/request_form.html",
        Client(SERVER_NAME=firm.host).get(reverse("dsr-submit")),
    )
    text = _text(body)

    # Then the notice leads, because both facts change what the reader does next
    assert "notice-info" in body, (
        f"the two conditions are prose rather than a notice, so they read as small "
        f"print above a form asking for a CPF: {text}"
    )
    assert "Não é preciso ter conta" in text, text
    assert "confira o endereço antes de enviar" in text, (
        f"nothing warns that the reply goes to the address typed here, and the view "
        f"keeps no other channel back to the requester: {text}"
    )

    # …and the two controls a stranger hesitates on are explained beside a mark that
    # says the section is about a person rather than about the request
    assert _glyph("user-round") in body, text
    assert "Quem pode pedir" in text, text
    assert "quem o represente" in text, (
        f"the relationship the form asks for is never explained, though it decides to "
        f"whom the answer may be given: {text}"
    )
    assert "apagável" in text, (
        f"the page asks for a CPF and does not say the record holding it can be "
        f"erased, which is the one thing `DataSubjectRequest` is built for: {text}"
    )

    # …and the form is still armed and still the point of the page
    assert CSRF_CONTROL in body, f"the intake form lost its CSRF control: {body}"
    assert "art. 18" in text, text


def test_the_receipt_marks_the_deadline_section_as_being_about_time(
    firm: Firm,
) -> None:
    """The one question the page answers with an article instead of a figure.

    Asserted alongside the heading rather than alone: no service window is written down
    in this repo, so a reader scanning for a date has to be steered to the section that
    explains why there is none, and the glyph may not be the only thing steering them.
    """
    body = _body(
        "lgpd/request_received.html",
        Client(SERVER_NAME=firm.host).get(reverse("dsr-received")),
    )
    text = _text(body)

    assert _glyph("calendar-clock") in body, (
        f"the deadline section carries no temporal mark: {text}"
    )
    assert "O que acontece agora" in text, text
    assert "art. 19" in text, (
        f"the mark points at a section that no longer names the article setting the "
        f"deadline, which is the only figure this page is entitled to give: {text}"
    )


# ---------------------------------------------------------- the invitation, composed


def test_the_acceptance_page_names_the_address_the_link_belongs_to(
    firm: Firm,
) -> None:
    """The address is the credential here, and it used to be an unlabelled grey line."""
    # Given a real invitation, opened by somebody with no session
    token = _issued_invite(firm.tenant)
    body = _body(
        "accounts/invite_accept.html",
        Client().get(reverse("invite-accept", args=[token])),
    )
    text = _text(body)

    # Then the address is presented as an identity rather than as loose text
    assert _glyph("user-round") in body, f"no identity mark beside the address: {text}"
    assert "Convite emitido para" in text, (
        f"the invited address is rendered with nothing saying what it is, so it reads "
        f"as the sender, the support desk, or an example: {text}"
    )
    assert INVITED in text, text

    # …and the three rules that produce `invite_refused.html` are stated BEFORE they
    # bite, which is the only moment they are useful
    assert "notice-info" in body, text
    assert "único uso" in text, text
    assert "prazo de validade" in text, text
    assert "aberto pelo endereço para o qual foi enviado" in text, (
        f"nothing says the link must be opened by the address it was sent to, and "
        f"`InviteEmailMismatchError` refuses every other one: {text}"
    )

    # …and the form the page exists for is still armed
    assert CSRF_CONTROL in body, f"the acceptance form lost its CSRF control: {body}"
    assert "Aceitar" in text, text


def test_the_refusal_marks_its_guidance_as_being_about_the_team(firm: Firm) -> None:
    """The way out of this page is a person, and the page may link to no page.

    Both readers it serves — an invitee holding a dead link, a firm user whose issue
    form was rejected — would be sent somewhere wrong by a single destination, so the
    mark stands in for one rather than leading to one.
    """
    body = _body(
        "accounts/invite_refused.html",
        Client().get(reverse("invite-accept", args=[DEAD_TOKEN])),
        HTTPStatus.NOT_FOUND,
    )
    text = _text(body)

    assert _glyph("users") in body, f"no team mark on the guidance section: {text}"
    assert "Pedir novo convite" in text, text
    assert "quem administra a equipe" in text, (
        f"the guidance no longer names who to ask, which is the only way forward this "
        f"page is allowed to offer: {text}"
    )

    # The alert above it stays unmarked on purpose: it already carries the error ramp,
    # the interrupting role and a sentence, and a fourth copy of one fact is noise.
    assert "notice-error" in body, text


def test_the_issued_page_keeps_the_mark_on_the_destination_it_offers(
    firm: Firm,
) -> None:
    """The precedent every other glyph in this wave follows, pinned where it started."""
    body = _body(
        "accounts/invite_issued.html",
        firm.as_owner().get(reverse("invite-issued")),
    )
    text = _text(body)

    assert _glyph("users") in body, (
        f"the link onward from the confirmation lost its mark: {text}"
    )
    assert f'href="{reverse("team")}"' in body, text
    assert "Ver a equipe" in text, text


# ------------------------------------------------------- the nav contract, both sides


def test_an_anonymous_visitor_draws_no_nav_on_any_page_this_wave_touched(
    firm: Firm,
) -> None:
    """Zero nav landmarks, on every public screen the composition pass reached.

    Each page is read through `_body` first, so a route that had started answering a
    redirect or a 500 could not pass this by carrying no markup at all.
    """
    anonymous = Client(SERVER_NAME=firm.host)
    platform = Client()

    pages = [
        ("lgpd/request_form.html", anonymous.get(reverse("dsr-submit")), HTTPStatus.OK),
        (
            "lgpd/request_received.html",
            anonymous.get(reverse("dsr-received")),
            HTTPStatus.OK,
        ),
        (
            "accounts/invite_accept.html",
            platform.get(reverse("invite-accept", args=[_issued_invite(firm.tenant)])),
            HTTPStatus.OK,
        ),
        (
            "accounts/invite_refused.html",
            platform.get(reverse("invite-accept", args=[DEAD_TOKEN])),
            HTTPStatus.NOT_FOUND,
        ),
    ]

    for page, response, expected in pages:
        _body(page, response, expected)
        document = _document(page, response, expected)
        assert NAV_OPEN.search(document) is None, (
            f"{page} draws a <nav> landmark for an anonymous visitor; the bar and the "
            f"trail both belong behind the sign-in guard in templates/base.html"
        )


def test_a_signed_in_accountant_keeps_the_shell_nav_on_the_same_public_route(
    firm: Firm,
) -> None:
    """The other half, and the one a fix for the half above would break.

    Same URL as the anonymous leg, so the difference between the two is the session and
    nothing else. Suppressing the bar on public routes would satisfy every anonymous
    assertion in this project and silently take the navigation away from anybody who
    follows the LGPD link in the footer while signed in.
    """
    route = reverse("dsr-submit")

    # Given the same page proven above to contribute no nav of its own
    anonymous = Client(SERVER_NAME=firm.host).get(route)
    _body("lgpd/request_form.html", anonymous)
    assert NAV_OPEN.search(anonymous.content.decode()) is None, (
        "the anonymous leg already fails, so a <nav> found for the accountant below "
        "could have come from the page rather than from the shell"
    )

    # When a staff accountant opens it
    response = firm.as_accountant().get(route)
    document = _document("lgpd/request_form.html", response, HTTPStatus.OK)

    # Then the shell still draws its bar…
    assert NAV_OPEN.search(document) is not None, (
        f"a signed-in accountant on {route} is given no <nav> landmark; the guard in "
        f"templates/base.html is `request.tenant and request.user.is_authenticated`, "
        f"and it must not have been narrowed to exclude a public route"
    )
    assert SHARED_NAV_ITEM in document, (
        f"the bar drawn on {route} omits {SHARED_NAV_ITEM!r}, the destination the "
        f"published matrix grants this role"
    )

    # …and the page's own composition is unchanged by the session
    text = _text(_body("lgpd/request_form.html", response))
    assert "Não é preciso ter conta" in text, text
    assert "Quem pode pedir" in text, text

"""The three pages that exist only to tell somebody what just happened.

Each one is the end of a flow — an invitation sent, an invitation refused, a rights
request filed — and each is reached by a redirect or an error path, so nobody browses
back to it and nobody notices when it stops saying anything. `invite_issued.html`
overrode no content block at all: the layout drew a heading, the `<main>` under it was
empty, and every structural guard in this suite stayed green while the one screen that
had to confirm the send confirmed nothing. A page whose body is missing renders a
perfectly valid document.

So these assertions are about the body, not the chrome. `_page_body` is the only way in
and it strips what `base.html` contributes to every page — the heading it owns, the
pre-rendered HTMX failure notice, the messages region — before asserting that anything
survives. Without that strip, "the main element is non-empty" is true of a template
that overrides nothing, which is exactly the state this file exists to catch.

The gate lives inside that helper rather than in a test of its own for the reason
`tests/ui/test_icons.py` gives about sentinels: `pytest -k` on any single rule here
does not select a sentinel, and the rule then reports success over an empty string.

The last rule is a different kind. It reads the LGPD confirmation for data the product
does not have — a protocol number, a response window in days — because the failure mode
of a page like this is not going blank a second time. It is somebody making it more
reassuring by inventing the reassurance: `docs/lgpd.md` documents no answering deadline
for a data subject (its one figure, two business days, is the incident runbook's clock
for notifying the ANPD) and the view supplies no identifier. A number here would be a
promise the product never made, and it would look exactly like a number that came from
somewhere.
"""

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

if TYPE_CHECKING:  # stubs-only: this type does not exist at runtime
    from django.test.client import _MonkeyPatchedWSGIResponse

from apps.tenants.models import TenantRole
from tests.ui.factories import Firm, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

MAIN: Final = re.compile(r"<main\b[^>]*>(.*?)</main>", re.DOTALL | re.IGNORECASE)

# The layout's own furniture, in the order `base.html` emits it. The messages region is
# the last thing before `{% block content %}`, so its close is where a page's own body
# begins — no page can move it, because no page renders it.
MESSAGES: Final = re.compile(
    r"""<div\b[^>]*\bid=["']messages["'][^>]*>""",
    re.IGNORECASE,
)

# Walked in pairs to find that close. Not `.find("</div>")`: the region nests two more
# divs per message, and `Firm.sign_in` drives the real allauth login, which enqueues
# one — so on the issued page, reached by redirect straight after signing in, the first
# close belongs to a notice and slicing there would hand a page's "body" the layout's
# own announcement. That reads as content, and would keep the gate below green for a
# template that renders nothing at all.
DIV: Final = re.compile(r"<div\b|</div\s*>", re.IGNORECASE)

TAGS: Final = re.compile(r"<[^>]+>")

# A span of days written as a promise. `\d+` and the word, in either order, so both
# "15 dias" and "prazo de 15 dias úteis" are caught.
DAYS: Final = re.compile(r"\d+\s*dias?\b", re.IGNORECASE)

INVITED: Final = "novo@confirmacao.example"
DEAD_TOKEN: Final = "este-token-nunca-foi-emitido"  # noqa: S105

# `resolve_invite` raises this for a token that matches no row. Asserted verbatim, so
# the test fails if the refusal page stops echoing the reason it was handed rather than
# merely stops being red.
NOT_FOUND_REASON: Final = "No invitation matches that link."


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    """Both hosts: the invitation is issued on a firm's subdomain, redeemed off it."""
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    return make_firm("alpha-confirma")


def _page_body(
    page: str,
    response: "_MonkeyPatchedWSGIResponse",
    expected_status: HTTPStatus,
) -> str:
    """Return what a page renders below the layout, refusing to return nothing.

    Three assertions guard the slice itself before the gate runs, because each failure
    would otherwise be reported as "this page says nothing" while the real fault is in
    this helper's idea of where the body starts.
    """
    assert response.status_code == expected_status, (
        f"{page}: answered {response.status_code}, wanted {expected_status} — the "
        f"assertions below would read the body of a different response"
    )
    html = response.content.decode()

    main = MAIN.search(html)
    assert main is not None, f"{page}: no <main> element in the response"
    inner = main.group(1)

    opening = MESSAGES.search(inner)
    assert opening is not None, (
        f"{page}: the layout's messages region is missing from <main>, so where the "
        f"page's own body begins is unknown"
    )
    close = _matching_close(inner, opening.end())
    assert close is not None, (
        f"{page}: the layout's messages region is never closed, so every byte of the "
        f"layout below it would be read as this page's own body"
    )

    body = inner[close:]
    assert _text(body), (
        f"{page}: renders nothing of its own below the layout — the heading, the "
        f"HTMX fallback and the messages region are all `base.html` writes, so this "
        f"page overrides no content block and its <main> is empty to a reader"
    )
    return body


def _matching_close(html: str, start: int) -> int | None:
    """Return the offset just past the `</div>` that closes the div open at `start`."""
    depth = 1
    for token in DIV.finditer(html, start):
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            return token.end()
    return None


def _text(html: str) -> str:
    """Return the readable text of a fragment, with whitespace collapsed."""
    return " ".join(TAGS.sub(" ", html).split())


# --------------------------------------------------------------- the invitation sent


def test_the_issued_page_confirms_the_send_and_leads_back_to_the_team(
    firm: Firm,
) -> None:
    """The redirect target of a successful issue says so, and offers somewhere to go.

    Driven through the real POST rather than fetched directly: this page is only ever
    reached by `issue_invite_view`'s redirect, and a confirmation that renders when
    fetched by hand but is never arrived at is not a confirmation.
    """
    # Given an invitation issued from the firm's team page
    response = firm.as_owner().post(
        reverse("invite-issue"),
        {"email": INVITED, "role": TenantRole.STAFF_ACCOUNTANT},
        follow=True,
    )

    # When the page it redirects to is read
    assert response.redirect_chain, "the issue view did not redirect anywhere"
    landed, _status = response.redirect_chain[-1]
    assert landed == reverse("invite-issued"), landed
    body = _page_body("accounts/invite_issued.html", response, HTTPStatus.OK)
    text = _text(body)

    # Then it states that the invitation went out…
    assert "notice-success" in body, (
        f"the send is not confirmed through the shared success notice: {text}"
    )
    assert "O convite foi enviado por e-mail" in text, text

    # …and leads to the one page that names the address it went to, since this one
    # cannot: the view renders it with an empty context.
    assert f'href="{reverse("team")}"' in body, (
        f"no link back to the team page, so the sender has no way to check the "
        f"address they typed: {body.strip()}"
    )
    assert INVITED not in text, (
        "the invited address appears on a page whose view passes no context — it "
        "could only have been guessed"
    )


# ------------------------------------------------------------ the invitation refused


def test_the_refused_page_alerts_with_the_reason_and_says_how_to_get_another() -> None:
    """The refusal keeps its interruption, and stops being a dead end.

    `role="alert"` is asserted on the element that carries the reason, not merely
    somewhere in the body: the guidance beneath it is ordinary prose, and an alert
    wrapped around the whole page would announce all of it at once.
    """
    # Given a link that matches no invitation
    response = Client().get(reverse("invite-accept", args=[DEAD_TOKEN]))

    # When the refusal is read
    body = _page_body("accounts/invite_refused.html", response, HTTPStatus.NOT_FOUND)
    text = _text(body)

    # Then the reason still interrupts, through the shared error notice
    alert = re.search(r"<div\b[^>]*\bclass=[\"'][^\"']*\bnotice-error\b[^>]*>", body)
    assert alert is not None, f"no error notice on the refusal page: {body.strip()}"
    assert 'role="alert"' in alert.group(0), (
        f"the refusal is announced as a status rather than an alert, so a screen "
        f"reader reaches it whenever it gets round to it: {alert.group(0)}"
    )
    assert NOT_FOUND_REASON in text, (
        f"the reason handed to the template is no longer rendered: {text}"
    )

    # …and the page now says what to do about it, which is the whole difference
    # between an error and an answer
    assert "Pedir novo convite" in text, text
    assert "um único uso" in text, text


# ------------------------------------------------------------- the rights request in


def test_the_lgpd_receipt_sets_an_expectation_it_can_keep() -> None:
    """Receipt, the deadline's source, who answers, and what survives an erasure."""
    # Given a rights request that has just been filed
    response = Client().get(reverse("dsr-received"))

    # When the confirmation is read
    body = _page_body("lgpd/request_received.html", response, HTTPStatus.OK)
    text = _text(body)

    # Then it confirms the filing…
    assert "notice-success" in body, body.strip()
    assert "encaminhada ao encarregado" in text, text

    # …names the deadline by the article that sets it, since no service window is
    # written down anywhere in this repo to quote instead
    assert "prazo legal" in text, text
    assert "art. 19" in text, text

    # …and answers the two questions this page used to leave to the reply itself
    assert "respondidos pelo próprio escritório" in text, text
    assert "guarda obrigatória por lei" in text, text


def test_the_lgpd_receipt_invents_neither_a_protocol_nor_a_service_window() -> None:
    """Reassurance the product cannot deliver is worse than none.

    `request_received_view` renders with an empty context, so any identifier on this
    page was made up by the template, and support would not recognise it. The same
    goes for a figure in days: `docs/lgpd.md` documents one — two business days — and
    it belongs to notifying the ANPD after an incident, not to answering a subject.
    """
    # Given the same confirmation
    body = _page_body(
        "lgpd/request_received.html",
        Client().get(reverse("dsr-received")),
        HTTPStatus.OK,
    )
    text = _text(body)

    # Then it offers no reference number to quote
    assert "protocolo" not in text.lower(), (
        f"a protocol is named on a page whose view passes no context, so it cannot "
        f"be the one recorded against the request: {text}"
    )

    # …and promises no span of days that no document in this repo backs
    invented = DAYS.search(text)
    assert invented is None, (
        f"a response window of `{invented.group(0) if invented else ''}` is promised "
        f"here; `docs/lgpd.md` sets none for a data subject, so cite the source of "
        f"the deadline or write the figure down there first: {text}"
    )

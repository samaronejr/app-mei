"""Neither invitation surface may put an English label in front of its reader.

Two screens, and neither reader is an English speaker by assumption. The portal
acceptance page is the first thing a MEI owner ever sees of this product, served to
somebody with no account and no session; the portal control on the client detail page
is where her accountant chooses which seat to hand her. Both are drawn by
`partials/pure/field.html` out of a `forms.Form`, which means their words come from
`apps/accounts/forms.py` rather than from a template anyone would think to read.

That is the whole reason this module exists. `locale/` holds no compiled catalogue, so
`gettext_lazy` returns its own msgid and a label declared as `_("full name")` renders
those two English words verbatim -- while looking, at the declaration site, exactly
like every correctly translated label beside it. Django's BUNDLED catalogue covers a
handful of them by coincidence (`password` arrives as `senha`, `email address` as
`endereco de email`), and that coincidence is what makes the untranslated ones so easy
to miss: the form looks half-Portuguese rather than obviously wrong.

WHERE THE FIX MAY LIVE, AND WHY IT MAY NOT LIVE AT THE MODEL.

The two seat names -- `MEI client owner` and `MEI client collaborator` -- are
`TenantRole` labels, and `Membership.role` and `Invite.role` both declare `choices=`
from that enum. Editing a label there rewrites serialized field metadata and the
migration autodetector answers with an `AlterField`. So the display names are mapped
at the FORM boundary instead, over `client_role_choices()`, and `makemigrations
--check --dry-run` stays clean. That is a constraint on the implementation, not on
this module: everything below is asserted on a RENDER, so adopting a real catalogue
later would change the mechanism and leave every assertion here standing.

THE SCAN, AND WHY IT HAS THE SHAPE IT HAS.

Markup is stripped FIRST and only then are the banned tokens matched as whole words,
which is the shape `tests/portal/test_portal_copy.py` proved. Both halves are needed.
Boundaries alone red on `<select name="role">` and `for="id_role"`, which are correct
markup that must not be translated -- and a scan that reds on correct markup gets
weakened until it means nothing. Stripping alone leaves pt-BR prose, over which a
substring test is a trap waiting for the first sentence containing one of these letter
runs inside a longer word.

Every absence assertion here is fenced by something positive. The helper refuses a
non-200, refuses a document whose form controls never rendered, and refuses a stripped
text missing the page's own heading; the structural cases pin each label and each
option to its exact field rather than merely looking for a word somewhere; and the
planted-positive case at the foot proves the matcher matches at all. An absence
asserted over a page that did not render is an absence from nothing.

FIRM-SIDE ROLE NAMES ARE OUT OF SCOPE HERE, DELIBERATELY.

`owner`, `staff accountant` and `operations admin` are the same untranslated
`TenantRole` labels, and they reach a reader through `templates/accounts/team.html`
(the members table, the pending-invite table and the firm invite form's own chooser),
through `templates/accounts/invite_confirm.html`, and through the Django admin form at
`apps/tenants/admin.py`. None of those is an invitation surface a MEI client sees, all
of them are read by accountants, and two of them go through `get_role_display()` --
which reads the model label directly and no form-boundary mapping can reach. Fixing
them is a separate decision about the model layer; this module neither asserts nor
excuses them.
"""

import html
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import issue_client_invite
from apps.tenants.models import TenantRole
from tests.ui.factories import Firm, add_client, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_URLCONF: Final = "apps.portal.urls"
FIRM_SLUG: Final = "acme"
INVITED: Final = "dona@padaria.example"
CLIENT_NAME: Final = "Padaria da Esquina MEI"
CLIENT_BASE: Final = "112223330001"

DETAIL_URL_NAME: Final = "client-detail"
PORTAL_ACCEPT_URL_NAME: Final = "portal-invite-accept"

# ------------------------------------------------------------------ the words expected
#
# Sentence case, matching the voice already shipped in the templates around these
# forms -- "Acesso ao portal", "Convite emitido para", and the `Papel` column heading
# `templates/accounts/team.html` already writes for the same concept. `Papel` is chosen
# rather than invented for that reason: the members table and the pending-invite table
# name this field that way today, and a form that called it something else would make
# one screen disagree with the next about what the reader is choosing.
#
# The two seat names say what the seat IS rather than transliterating the stored value.
# "Titular" is the word a MEI owner meets on her own Receita documents; "Colaborador"
# is the person she lets in beside her. Neither carries a gendered parenthetical: the
# product speaks to one reader at a time and "Colaborador(a)" is a form to fill in, not
# a sentence to read.
FULL_NAME_LABEL: Final = "Nome completo"
PASSWORD_CONFIRMATION_LABEL: Final = "Confirmação da senha"  # noqa: S105
ROLE_LABEL: Final = "Papel"

PORTAL_SEAT_LABELS: Final[dict[str, str]] = {
    TenantRole.CLIENT_OWNER.value: "Titular do MEI",
    TenantRole.CLIENT_COLLABORATOR.value: "Colaborador do MEI",
}

# ------------------------------------------------------------------- the words refused
#
# Exactly the msgids `gettext_lazy` hands back untranslated on these two screens,
# written out rather than imported. Importing them would track a rename and miss the
# point: what is refused is the ENGLISH, and a label rewritten in English under a new
# msgid must red here too.
ENGLISH_LABELS: Final[tuple[str, ...]] = (
    "role",
    "full name",
    "password confirmation",
    "MEI client owner",
    "MEI client collaborator",
)

# ------------------------------------------------------------------------- the stripper

SCRIPT_OR_STYLE: Final = re.compile(
    r"<(script|style)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
HTML_COMMENT: Final = re.compile(r"<!--.*?-->", re.DOTALL)
TAG: Final = re.compile(r"<[^>]*>")
RUN_OF_SPACE: Final = re.compile(r"\s+")

LABEL_FOR: Final = re.compile(
    r'<label[^>]*\bfor="(?P<field>[^"]+)"[^>]*>(?P<text>.*?)</label>',
    re.DOTALL | re.IGNORECASE,
)
OPTION: Final = re.compile(
    r'<option value="(?P<value>[^"]*)"[^>]*>(?P<text>.*?)</option>',
    re.DOTALL | re.IGNORECASE,
)
H1: Final = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)

# The required marker the field partial appends inside the label element. Stripped off
# the label text so the pins below name the word rather than the punctuation.
REQUIRED_MARK: Final = "*"


@dataclass(frozen=True)
class Surface:
    """One rendered invitation screen, with everything needed to trust a scan of it."""

    label: str
    document: str
    # A string the stripper must leave behind. It is the page's own heading, so a
    # document that answered 200 out of some fallback branch cannot pass as this page.
    sentinel: str
    # Raw-markup markers proving the FORM CONTROLS rendered. Absence assertions over a
    # page whose form vanished are absences from nothing, and a `{% can %}` that
    # answered False, a form instantiated as None, or an empty choice list all produce
    # exactly that page.
    controls: tuple[str, ...]


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def firm() -> Firm:
    """One firm, one MEI client, and an owner who may hand out portal seats."""
    made = make_firm(FIRM_SLUG)
    add_client(made, legal_name=CLIENT_NAME, base=CLIENT_BASE)
    return made


# ------------------------------------------------------------------------- the surfaces


def _portal_accept_surface(firm: Firm) -> Surface:
    """Render the acceptance screen the way an invitee reaches it: no session at all.

    An anonymous GET is what takes the view down its new-account branch, which is the
    only branch that renders `InviteAcceptForm` and therefore the only one carrying the
    two labels under test. A signed-in visitor confirms instead and sees no fields.
    """
    _invite, raw_token = issue_client_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        client=firm.clients[0],
        email=INVITED,
        role=TenantRole.CLIENT_OWNER,
    )
    target = str(
        reverse(PORTAL_ACCEPT_URL_NAME, args=[raw_token], urlconf=PORTAL_URLCONF),
    )
    response = Client().get(target, headers={"host": f"{FIRM_SLUG}-portal.localhost"})

    assert response.status_code == HTTPStatus.OK, (
        f"the acceptance screen answered {response.status_code}; a refusal page "
        f"carries none of the labels this module is about, and satisfies every "
        f"absence asserted below"
    )
    return Surface(
        label="portal acceptance screen",
        document=response.content.decode(),
        sentinel=f"Convite de {firm.tenant.name}",
        controls=('for="id_full_name"', 'for="id_password2"', 'name="password2"'),
    )


def _client_detail_surface(firm: Firm) -> Surface:
    """Render the client detail screen as the owner, who holds `users.create`.

    The invite control sits behind `{% can user "users.create" %}`, so an account
    without it is served the same page with the whole section missing -- 200, styled,
    and carrying no English because it carries no form.
    """
    target = str(reverse(DETAIL_URL_NAME, args=[firm.clients[0].pk]))
    response = firm.as_owner().get(target)

    assert response.status_code == HTTPStatus.OK, (
        f"{target} answered {response.status_code}, so nothing asserted about it is a "
        f"statement about a rendered page"
    )
    return Surface(
        label="client detail portal invite control",
        document=response.content.decode(),
        sentinel="Acesso ao portal",
        controls=(
            'for="id_role"',
            'value="client_owner"',
            'value="client_collaborator"',
        ),
    )


# -------------------------------------------------------------------------- the helpers


def _visible_text(surface: Surface) -> str:
    """Return only what a reader sees, refusing a document that renders nothing.

    Three gates, and each closes a different way for this scan to pass while the
    product is broken: a document whose form never rendered, a document the stripper
    threw away, and a document that is some other page answering 200.
    """
    missing = [marker for marker in surface.controls if marker not in surface.document]
    assert missing == [], (
        f"{surface.label}: the form controls {missing} are absent from the document, "
        f"so the fields whose labels this module scans were never rendered and every "
        f"English word it refuses is missing for the wrong reason"
    )

    without_code = HTML_COMMENT.sub(" ", SCRIPT_OR_STYLE.sub(" ", surface.document))
    text = RUN_OF_SPACE.sub(" ", html.unescape(TAG.sub(" ", without_code))).strip()

    assert text, (
        f"{surface.label}: stripping the markup left no text at all, so every token "
        f"this scan refuses is absent from an empty string"
    )
    assert surface.sentinel in text, (
        f"{surface.label}: {surface.sentinel!r} is missing from the stripped text, so "
        f"either the stripper threw the page's own content away or this is not the "
        f"page it claims to be"
    )
    return text


def _english_label_tokens(text: str) -> list[str]:
    """Return every refused English label appearing as a WORD in what a reader sees."""
    return sorted(
        token
        for token in ENGLISH_LABELS
        if re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE)
    )


def _plain(fragment: str) -> str:
    """Reduce one markup fragment to the words inside it, without the required mark."""
    text = RUN_OF_SPACE.sub(" ", html.unescape(TAG.sub(" ", fragment))).strip()
    return text.removesuffix(REQUIRED_MARK).strip()


def _labels_by_field(surface: Surface) -> dict[str, str]:
    """Return each field id on the page mapped to the words its own label carries."""
    found = {
        match.group("field"): _plain(match.group("text"))
        for match in LABEL_FOR.finditer(surface.document)
    }
    assert found, (
        f"{surface.label}: the document carries no label element bound to any field, "
        f"so there is nothing here to be in Portuguese or in English"
    )
    return found


def _options_by_value(surface: Surface, *, name: str) -> dict[str, str]:
    """Return the chooser's stored values mapped to the words offered beside them."""
    selects = surface.document.split(f'name="{name}"')
    assert len(selects) == 2, (
        f"{surface.label}: the document holds {len(selects) - 1} controls named "
        f"{name!r} rather than exactly one, so the options read below belong to an "
        f"unknown chooser"
    )
    chooser = selects[1].split("</select>")[0]
    return {
        match.group("value"): _plain(match.group("text"))
        for match in OPTION.finditer(chooser)
    }


# ------------------------------------------------------- what each field calls itself


def test_the_acceptance_screen_names_both_of_its_fields_in_portuguese(
    firm: Firm,
) -> None:
    """The first screen of the product, and the two labels it got wrong.

    Pinned per FIELD rather than as words somewhere on the page. A page-wide presence
    test stays green when the right words land on the wrong control, which on a form
    carrying two password boxes is precisely the mistake worth catching.
    """
    surface = _portal_accept_surface(firm)

    labels = _labels_by_field(surface)

    assert labels.get("id_full_name") == FULL_NAME_LABEL, (
        f"the name field is labelled {labels.get('id_full_name')!r} rather than "
        f"{FULL_NAME_LABEL!r}"
    )
    assert labels.get("id_password2") == PASSWORD_CONFIRMATION_LABEL, (
        f"the confirmation field is labelled {labels.get('id_password2')!r} rather "
        f"than {PASSWORD_CONFIRMATION_LABEL!r}"
    )


def test_the_portal_invite_control_names_its_chooser_and_both_seats_in_portuguese(
    firm: Firm,
) -> None:
    """The seat names are the half a form-boundary map has to carry.

    The label is one declaration; the two option names are the `TenantRole` labels the
    model may not translate, mapped over `client_role_choices()` at the form. Asserting
    the mapping as a whole dict is what makes it falsifiable: a map that lost an entry
    would fall back to the English it was written to replace, and a map that gained a
    stored value would offer a seat the CHECK constraint refuses.
    """
    surface = _client_detail_surface(firm)

    assert _labels_by_field(surface).get("id_role") == ROLE_LABEL, (
        f"the seat chooser is labelled {_labels_by_field(surface).get('id_role')!r} "
        f"rather than {ROLE_LABEL!r}"
    )
    assert _options_by_value(surface, name="role") == PORTAL_SEAT_LABELS, (
        f"the chooser offers {_options_by_value(surface, name='role')} rather than "
        f"{PORTAL_SEAT_LABELS}; an entry that fell back to its incoming label is the "
        f"model's own English word reaching an accountant choosing a seat for a client"
    )


# ------------------------------------------------------------ the English never, either


def test_the_acceptance_screen_shows_no_english_label_word(firm: Firm) -> None:
    """`full name` and `password confirmation`, refused in what a reader can read."""
    text = _visible_text(_portal_accept_surface(firm))

    assert _english_label_tokens(text) == [], (
        f"the acceptance screen shows an invitee the English label word(s) "
        f"{_english_label_tokens(text)}. `locale/` compiles no catalogue, so a msgid "
        f"left in English is rendered in English on the first screen this product "
        f"ever shows a MEI owner"
    )


def test_the_portal_invite_control_shows_no_english_label_word(firm: Firm) -> None:
    """`role` and both `MEI client ...` seat names, refused the same way.

    This is the case that would still red if the seat names were mapped nowhere, or
    mapped somewhere the detail page does not read -- the label pins above check the
    field this module knows about, and this one checks the whole visible page.
    """
    text = _visible_text(_client_detail_surface(firm))

    assert _english_label_tokens(text) == [], (
        f"the client detail screen shows the English label word(s) "
        f"{_english_label_tokens(text)} to the accountant choosing a portal seat"
    )


def test_the_scan_would_notice_an_english_label_if_one_were_there() -> None:
    """The control for both cases above: prove the matcher matches.

    The fragment carries one banned word in its TEXT and three decoys in its MARKUP:
    a control named `role`, a label bound to `id_role`, and a stored value spelled
    `client_owner`. All three are correct markup that must never be translated, and a
    scan that reddened on them would be narrowed until it found nothing anywhere.
    """
    planted = (
        '<label class="field-label" for="id_role">Papel</label>'
        '<select name="role" id="id_role">'
        '<option value="client_owner">Titular do MEI</option></select>'
        "<p>Campo obrigatório: full name</p>"
    )
    surface = Surface(
        label="planted",
        document=planted,
        sentinel="Titular do MEI",
        controls=('for="id_role"',),
    )

    tokens = _english_label_tokens(_visible_text(surface))

    assert tokens == ["full name"], (
        f"the scan found {tokens} in a fragment carrying exactly one planted English "
        f"label in its text and three decoys in its markup; either it misses the "
        f"words it exists to refuse, or it reds on the attributes and stored values "
        f"that legitimately spell them -- and both make the cases above worthless"
    )

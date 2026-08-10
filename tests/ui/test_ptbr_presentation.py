import html
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import pytest
from django.template import Context, Template
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts import forms as accounts_forms
from apps.accounts.models import User
from apps.clients.models import ClientStatus, GovBrTrustLevel, OnboardingStatus
from apps.core.templatetags import ptbr
from apps.obligations.models import ObligationStatus
from apps.tenants.models import Invite, TenantRole
from tests.support import enrol_totp
from tests.ui.factories import Firm, add_member, make_firm

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

FILTER_CASES: Final[tuple[tuple[str, str, str], ...]] = (
    ("papel", TenantRole.OWNER.value, "Proprietário"),
    ("papel", TenantRole.STAFF_ACCOUNTANT.value, "Contador"),
    (
        "papel",
        TenantRole.OPERATIONS_ADMIN.value,
        "Administrador de operações",
    ),
    ("papel", TenantRole.CLIENT_OWNER.value, "Titular do MEI"),
    (
        "papel",
        TenantRole.CLIENT_COLLABORATOR.value,
        "Colaborador do MEI",
    ),
    ("situacao_cliente", ClientStatus.ONBOARDING.value, "Em onboarding"),
    ("situacao_cliente", ClientStatus.ACTIVE.value, "Ativo"),
    ("situacao_cliente", ClientStatus.SUSPENDED.value, "Suspenso"),
    ("situacao_cliente", ClientStatus.CLOSED.value, "Encerrado"),
    (
        "situacao_obrigacao",
        ObligationStatus.SCHEDULED.value,
        "Programada",
    ),
    ("situacao_obrigacao", ObligationStatus.DUE.value, "A pagar"),
    ("situacao_obrigacao", ObligationStatus.PAID.value, "Paga"),
    ("situacao_obrigacao", ObligationStatus.OVERDUE.value, "Em atraso"),
    ("situacao_obrigacao", ObligationStatus.WAIVED.value, "Dispensada"),
    ("situacao_onboarding", OnboardingStatus.PENDING.value, "Pendente"),
    ("situacao_onboarding", OnboardingStatus.BLOCKED.value, "Travado"),
    ("situacao_onboarding", OnboardingStatus.DONE.value, "Concluído"),
    (
        "situacao_onboarding",
        OnboardingStatus.NOT_APPLICABLE.value,
        "Não se aplica",
    ),
    ("nivel_govbr", GovBrTrustLevel.UNKNOWN.value, "Não informado"),
    ("nivel_govbr", GovBrTrustLevel.BRONZE.value, "Bronze"),
    ("nivel_govbr", GovBrTrustLevel.PRATA.value, "Prata"),
    ("nivel_govbr", GovBrTrustLevel.OURO.value, "Ouro"),
)


def test_papel_map_covers_every_tenant_role() -> None:
    labels = getattr(ptbr, "TENANT_ROLE_LABELS", {})
    assert "papel" in ptbr.register.filters, "the papel filter does not exist"
    missing = [
        f"TenantRole.{member.name}"
        for member in TenantRole
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in TenantRole})
    assert set(labels) == {member.value for member in TenantRole}, (
        f"TENANT_ROLE_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_situacao_cliente_map_covers_every_client_status() -> None:
    labels = getattr(ptbr, "CLIENT_STATUS_LABELS", {})
    assert "situacao_cliente" in ptbr.register.filters, (
        "the situacao_cliente filter does not exist"
    )
    missing = [
        f"ClientStatus.{member.name}"
        for member in ClientStatus
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in ClientStatus})
    assert set(labels) == {member.value for member in ClientStatus}, (
        f"CLIENT_STATUS_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_situacao_obrigacao_map_covers_every_obligation_status() -> None:
    labels = getattr(ptbr, "OBLIGATION_STATUS_LABELS", {})
    assert "situacao_obrigacao" in ptbr.register.filters, (
        "the situacao_obrigacao filter does not exist"
    )
    missing = [
        f"ObligationStatus.{member.name}"
        for member in ObligationStatus
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in ObligationStatus})
    assert set(labels) == {member.value for member in ObligationStatus}, (
        f"OBLIGATION_STATUS_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_situacao_onboarding_map_covers_every_onboarding_status() -> None:
    labels = getattr(ptbr, "ONBOARDING_STATUS_LABELS", {})
    assert "situacao_onboarding" in ptbr.register.filters, (
        "the situacao_onboarding filter does not exist"
    )
    missing = [
        f"OnboardingStatus.{member.name}"
        for member in OnboardingStatus
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in OnboardingStatus})
    assert set(labels) == {member.value for member in OnboardingStatus}, (
        f"ONBOARDING_STATUS_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_nivel_govbr_map_covers_every_gov_br_trust_level() -> None:
    labels = getattr(ptbr, "GOVBR_TRUST_LEVEL_LABELS", {})
    assert "nivel_govbr" in ptbr.register.filters, (
        "the nivel_govbr filter does not exist"
    )
    missing = [
        f"GovBrTrustLevel.{member.name}"
        for member in GovBrTrustLevel
        if member.value not in labels
    ]
    unexpected = sorted(set(labels) - {member.value for member in GovBrTrustLevel})
    assert set(labels) == {member.value for member in GovBrTrustLevel}, (
        f"GOVBR_TRUST_LEVEL_LABELS is missing {missing} and has unexpected {unexpected}"
    )


def test_the_recorded_tier_never_reads_as_the_word_for_an_unmapped_value() -> None:
    """`Não informado` and `Desconhecido` must stay two different sentences.

    UNKNOWN is a real, default, extremely common state -- `GovBrTrustLevel`'s docstring
    calls it "nobody has asked". `Desconhecido` is what every filter in this family
    renders for a value NO map knows, which is a defect. Collapsing the two would hide
    the second inside the first on the one card where both could appear.
    """
    recorded = ptbr.register.filters["nivel_govbr"](GovBrTrustLevel.UNKNOWN.value)
    unmapped = ptbr.register.filters["nivel_govbr"]("tier_that_does_not_exist")

    assert recorded != unmapped, (
        f"a recorded-but-unasked gov.br tier and a value no map knows both render "
        f"{recorded!r}, so an unmapped member is indistinguishable from the default"
    )
    assert unmapped == "Desconhecido", unmapped


@pytest.mark.parametrize(("filter_name", "value", "expected"), FILTER_CASES)
def test_each_filter_renders_the_approved_word(
    filter_name: str,
    value: str,
    expected: str,
) -> None:
    assert ptbr.register.filters[filter_name](value) == expected


@pytest.mark.parametrize(
    "filter_name",
    [
        "papel",
        "situacao_cliente",
        "situacao_obrigacao",
        "situacao_onboarding",
        "nivel_govbr",
    ],
)
def test_unmapped_values_render_desconhecido_without_disclosing_the_raw_value(
    filter_name: str,
) -> None:
    raw_value = "internal_future_value"
    rendered = ptbr.register.filters[filter_name](raw_value)

    assert rendered == "Desconhecido"
    assert raw_value not in rendered


def test_the_filters_are_available_to_templates() -> None:
    planted = (
        "{% load ptbr %}"
        "{{ role|papel }}|"
        "{{ client|situacao_cliente }}|"
        "{{ obligation|situacao_obrigacao }}|"
        "{{ onboarding|situacao_onboarding }}"
    )
    rendered = Template(planted).render(
        Context(
            {
                "role": TenantRole.OWNER.value,
                "client": ClientStatus.ACTIVE.value,
                "obligation": ObligationStatus.DUE.value,
                "onboarding": OnboardingStatus.BLOCKED.value,
            }
        )
    )

    assert rendered == "Proprietário|Ativo|A pagar|Travado"


# ---------------------------------------------------- the five reader-facing surfaces
#
# The filters above are a mechanism. Everything below is asserted on a RENDER, because
# a mechanism nobody calls translates nothing. Five surfaces name a `TenantRole` to a
# reader and each one reaches the word by a different route, which is why one fix could
# not cover them: two `team.html` cells and one `invite_confirm.html` paragraph go
# through `get_role_display()` (the model's own label, unreachable from a form), the
# firm invite chooser goes through `firm_role_choices()` at the form boundary, and the
# Django admin renders the field itself with no template of ours in between.
#
# Every absence is fenced by something positive: each surface is refused unless it
# answered 200 and carries the cells, options or filter links the assertion reads, so
# an English word cannot be missing because the page never rendered.

FIRM_SLUG: Final = "papeis-equipe"
INVITED: Final = "convidada@example.com"
OPERATOR: Final = "operador@plataforma.example"

FIRM_ROLE_WORDS: Final[dict[str, str]] = {
    TenantRole.OWNER.value: "Proprietário",
    TenantRole.STAFF_ACCOUNTANT.value: "Contador",
    TenantRole.OPERATIONS_ADMIN.value: "Administrador de operações",
}
EVERY_ROLE_WORD: Final[dict[str, str]] = {
    **FIRM_ROLE_WORDS,
    TenantRole.CLIENT_OWNER.value: "Titular do MEI",
    TenantRole.CLIENT_COLLABORATOR.value: "Colaborador do MEI",
}

# The msgids `gettext_lazy` hands back untranslated, written out rather than imported.
# What is refused is the ENGLISH: a label rewritten in English under a new msgid must
# red here too.
ENGLISH_ROLE_WORDS: Final[tuple[str, ...]] = (
    "owner",
    "staff accountant",
    "operations admin",
    "MEI client owner",
    "MEI client collaborator",
)

SCRIPT_OR_STYLE: Final = re.compile(
    r"<(script|style)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
HTML_COMMENT: Final = re.compile(r"<!--.*?-->", re.DOTALL)
TAG: Final = re.compile(r"<[^>]*>")
RUN_OF_SPACE: Final = re.compile(r"\s+")

# The team table's role cell, found by the column name it regrows on a phone rather
# than by position -- `.row-stack` makes `data-label` the one attribute every cell in
# that column is guaranteed to carry.
TEAM_ROLE_CELL: Final = re.compile(
    r'<td[^>]*data-label="Papel"[^>]*>(?P<text>.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)
OPTION: Final = re.compile(
    r'<option value="(?P<value>[^"]*)"[^>]*>(?P<text>.*?)</option>',
    re.DOTALL | re.IGNORECASE,
)
LABEL_FOR: Final = re.compile(
    r'<label[^>]*\bfor="(?P<field>[^"]+)"[^>]*>(?P<text>.*?)</label>',
    re.DOTALL | re.IGNORECASE,
)
# The changelist column Django names after the callable that draws it, and the filter
# links it builds out of that filter's own lookups.
ADMIN_ROLE_CELL: Final = re.compile(
    r'<t[dh] class="field-role(?:_\w+)?">(?P<text>.*?)</t[dh]>',
    re.DOTALL | re.IGNORECASE,
)
ADMIN_ROLE_FILTER_LINK: Final = re.compile(
    r'<a href="\?[^"]*\brole=(?P<value>\w+)[^"]*"[^>]*>(?P<text>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

# The required marker `partials/pure/field.html` appends INSIDE the label element.
REQUIRED_MARK: Final = "*"

# An address, which the shipped factory spells `owner@<slug>.example.com` on every
# firm it builds. Matched loosely on purpose: this is a thing to EXCLUDE from a prose
# scan, so over-matching one token costs nothing and under-matching costs a false red.
EMAIL_ADDRESS: Final = re.compile(r"\S+@\S+")

MEMBERSHIP_CHANGELIST: Final = "/admin/tenants/membership/"
INVITE_CHANGELIST: Final = "/admin/tenants/invite/"
INVITE_ADD_FORM: Final = "/admin/tenants/invite/add/"


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def _admin_urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.admin.urls"


def _plain(fragment: str) -> str:
    """Reduce one markup fragment to the words inside it, without the required mark."""
    without_code = HTML_COMMENT.sub(" ", SCRIPT_OR_STYLE.sub(" ", fragment))
    text = RUN_OF_SPACE.sub(" ", html.unescape(TAG.sub(" ", without_code))).strip()
    return text.removesuffix(REQUIRED_MARK).strip()


def _english_words(text: str) -> list[str]:
    """Return every refused English role name appearing as a WORD in the given text.

    Whole words over stripped text, which is the shape `tests/ui/test_invitation_
    labels.py` proved: boundaries alone red on `value="owner"`, which is correct markup
    that must never be translated, and a substring scan reds on the letters of a longer
    pt-BR word.

    Email addresses are dropped first, for the same reason that scan drops markup. A
    word boundary sits either side of the local part of `owner@acme.example.com`, so
    `\\bowner\\b` matches an address the product is required to render verbatim -- and
    every firm the shipped factory builds signs its owner in under exactly that one.
    An address is a stored identifier, never prose, and nothing in it is translatable.
    `test_the_scan_still_notices_an_english_role_beside_an_address` is the fence that
    keeps this from quietly becoming a hole.
    """
    prose = EMAIL_ADDRESS.sub(" ", text)
    return sorted(
        word
        for word in ENGLISH_ROLE_WORDS
        if re.search(rf"\b{re.escape(word)}\b", prose, re.IGNORECASE)
    )


def _body(response: "_MonkeyPatchedWSGIResponse", what: str) -> str:
    assert response.status_code == HTTPStatus.OK, (
        f"{what} answered {response.status_code}, so nothing asserted about it is a "
        f"statement about a rendered page"
    )
    return str(response.content.decode())


@pytest.fixture
def firm() -> Firm:
    """A firm holding all three firm-side roles, with one invitation outstanding."""
    made = make_firm(FIRM_SLUG)
    add_member(
        made.tenant,
        f"ops@{FIRM_SLUG}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )
    return made


@pytest.fixture
def pending_invite(firm: Firm) -> Invite:
    invite, _token = Invite.issue(
        tenant=firm.tenant,
        email=INVITED,
        role=TenantRole.OPERATIONS_ADMIN,
    )
    return invite


def _team_halves(firm: Firm) -> tuple[str, str]:
    """Return the team page split at the pending-invites heading.

    Both tables label their role column `Papel`, so a scan of the whole document could
    not say WHICH table carried a word. The split is on the heading's own id, and the
    halves are checked to be non-degenerate before either is read.
    """
    body = _body(firm.as_owner().get(reverse("team")), "the team screen")
    members, separator, invites = body.partition('id="convites"')
    assert separator, (
        "the pending-invites heading is not on the team page, so the document cannot "
        "be split into the two tables this module reads separately"
    )
    assert INVITED in invites, (
        f"the outstanding invite for {INVITED} is not in the second half of the page, "
        f"so the invites table fell to its empty state and every assertion about an "
        f"invite row is a statement about nothing"
    )
    return members, invites


def _admin_session() -> Client:
    """A platform operator with no membership, so the console is reached directly."""
    operator = User.objects.create_superuser(email=OPERATOR)
    enrol_totp(operator)
    session = Client()
    session.force_login(operator)
    return session


# ------------------------------------------------- (1) the team page's members table


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("pending_invite")
def test_the_members_table_names_every_role_in_portuguese(firm: Firm) -> None:
    # Given a firm holding all three firm-side roles
    members, _invites = _team_halves(firm)

    # When the role column is read
    cells = sorted(
        {_plain(match.group("text")) for match in TEAM_ROLE_CELL.finditer(members)},
    )

    # Then each row names its role in the words an accountant uses
    assert cells == sorted(FIRM_ROLE_WORDS.values()), (
        f"the members table names its roles {cells} rather than "
        f"{sorted(FIRM_ROLE_WORDS.values())}"
    )
    assert _english_words(_plain(members)) == [], (
        f"the members table shows the English role word(s) "
        f"{_english_words(_plain(members))} to the firm's own staff"
    )


# ------------------------------------------------ (2) the team page's pending invites


@pytest.mark.django_db(transaction=True)
def test_the_pending_invites_table_names_the_offered_role_in_portuguese(
    firm: Firm,
    pending_invite: Invite,
) -> None:
    # Given one invitation outstanding for an operations admin
    assert pending_invite.role == TenantRole.OPERATIONS_ADMIN
    _members, invites = _team_halves(firm)

    # When the role column of the second table is read
    cells = [_plain(match.group("text")) for match in TEAM_ROLE_CELL.finditer(invites)]

    # Then the seat being offered is named in Portuguese
    assert cells == [FIRM_ROLE_WORDS[TenantRole.OPERATIONS_ADMIN.value]], (
        f"the pending-invites table names the offered role {cells} rather than "
        f"{[FIRM_ROLE_WORDS[TenantRole.OPERATIONS_ADMIN.value]]}"
    )


# -------------------------------------------------- (3) the revoke confirmation prose


@pytest.mark.django_db(transaction=True)
def test_the_revoke_confirmation_names_the_offered_role_in_portuguese(
    firm: Firm,
    pending_invite: Invite,
) -> None:
    """The one surface where the role is read inside a sentence, not a table cell."""
    # Given the confirmation screen for that invitation
    target = reverse("invite-revoke", args=[pending_invite.pk])
    text = _plain(
        _body(firm.as_owner().get(target), "the revoke confirmation screen"),
    )

    # When the sentence naming the seat is read — the page's own words, so a document
    # that answered 200 out of some other branch cannot satisfy this
    expected = (
        f"Este convite oferecia o papel de "
        f"{FIRM_ROLE_WORDS[TenantRole.OPERATIONS_ADMIN.value]} e valeria até"
    )

    # Then the owner deciding whether to withdraw it reads a Portuguese role name
    assert expected in text, f"{expected!r} is not in the confirmation prose: {text!r}"
    assert _english_words(text) == [], (
        f"the revoke confirmation shows the English role word(s) "
        f"{_english_words(text)} to the owner withdrawing the invitation"
    )


# --------------------------------------------------------- (4) the firm invite chooser


@pytest.mark.django_db(transaction=True)
def test_the_firm_invite_chooser_names_every_seat_it_offers_in_portuguese(
    firm: Firm,
) -> None:
    """Asserted as a whole dict, which is what makes the mapping falsifiable.

    An entry that fell back to its incoming label is the model's own English word in
    front of the owner choosing a seat; an entry that gained a stored value would offer
    a role `invite_role_is_firm_side` refuses.
    """
    # Given the team screen, whose sidebar carries the invitation form
    body = _body(firm.as_owner().get(reverse("team")), "the team screen")
    choosers = body.split('name="role"')
    assert len(choosers) == 2, (
        f"the page holds {len(choosers) - 1} controls named 'role' rather than exactly "
        f"one, so the options read below belong to an unknown chooser"
    )
    chooser = choosers[1].split("</select>")[0]

    # When the offered seats are read
    offered = {
        match.group("value"): _plain(match.group("text"))
        for match in OPTION.finditer(chooser)
    }

    # Then each is named in Portuguese, and the label above them already was
    assert offered == FIRM_ROLE_WORDS, f"the chooser offers {offered}"
    labels = {
        match.group("field"): _plain(match.group("text"))
        for match in LABEL_FOR.finditer(body)
    }
    assert labels.get("id_role") == "Papel", labels.get("id_role")


# ---------------------------------------------------------------- (5) the admin console


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_admin_urls")
def test_the_admin_membership_changelist_names_each_role_in_portuguese(
    firm: Firm,
) -> None:
    """Django draws `list_display` itself, so only a display callable can reach it."""
    # Given the three firm-side memberships and a platform operator
    body = _body(
        _admin_session().get(MEMBERSHIP_CHANGELIST),
        MEMBERSHIP_CHANGELIST,
    )
    assert firm.owner.email in body, (
        f"{MEMBERSHIP_CHANGELIST} lists none of this firm's rows, so the role column "
        f"read below is empty and every English word is absent from nothing"
    )

    # When the role column is read
    cells = sorted(
        {_plain(match.group("text")) for match in ADMIN_ROLE_CELL.finditer(body)},
    )

    # Then the console names each role the way the product does
    assert cells == sorted(FIRM_ROLE_WORDS.values()), (
        f"the membership changelist names its roles {cells} rather than "
        f"{sorted(FIRM_ROLE_WORDS.values())}"
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_admin_urls")
def test_the_admin_invite_changelist_and_its_filter_name_roles_in_portuguese(
    firm: Firm,
    pending_invite: Invite,
) -> None:
    """The filter sidebar is a second rendering of the same word on the same page."""
    # Given one pending invitation
    assert pending_invite.tenant_id == firm.tenant.id
    body = _body(_admin_session().get(INVITE_CHANGELIST), INVITE_CHANGELIST)
    assert INVITED in body, (
        f"{INVITE_CHANGELIST} lists no invitation, so the role column read below is "
        f"empty"
    )

    # When the column and the filter links are read
    cells = [_plain(match.group("text")) for match in ADMIN_ROLE_CELL.finditer(body)]
    offered = {
        match.group("value"): _plain(match.group("text"))
        for match in ADMIN_ROLE_FILTER_LINK.finditer(body)
    }

    # Then both name the role in Portuguese — the column for the row that exists…
    assert cells == [FIRM_ROLE_WORDS[TenantRole.OPERATIONS_ADMIN.value]], cells
    # …and the sidebar for every role the field can hold
    assert offered == EVERY_ROLE_WORD, f"the role filter offers {offered}"


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("_admin_urls")
def test_the_admin_invite_form_names_its_role_field_and_options_in_portuguese() -> None:
    """`label=_("role")` was the last English form label outside Django's own."""
    # Given the invitation add form
    body = _body(_admin_session().get(INVITE_ADD_FORM), INVITE_ADD_FORM)

    # When its role control is read
    labels = {
        match.group("field"): _plain(match.group("text")).removesuffix(":").strip()
        for match in LABEL_FOR.finditer(body)
    }
    chooser = body.split('name="role"')[1].split("</select>")[0]
    offered = {
        match.group("value"): _plain(match.group("text"))
        for match in OPTION.finditer(chooser)
    }

    # Then the field and every seat it offers are named in Portuguese, asserted
    # together so a red run reports both leaks rather than stopping at the first
    assert (labels.get("id_role"), offered) == ("Papel", FIRM_ROLE_WORDS), (
        f"the admin invite form labels its role field {labels.get('id_role')!r} and "
        f"offers {offered}"
    )


# ---------------------------------------------- one word list, not two that agree today


def test_the_scan_still_notices_an_english_role_beside_an_address() -> None:
    """The control for every absence above: prove the matcher still matches.

    The fragment carries one banned word in its prose and three decoys around it -- an
    address whose local part IS a role name, a stored value spelled `operations_admin`,
    and a pt-BR word containing the letters of another. All three are things the
    product must render verbatim, and a scan that reddened on any of them would be
    narrowed until it found nothing anywhere.
    """
    planted = (
        '<td data-label="Pessoa">owner@acme.example.com</td>'
        '<td data-label="Papel">Proprietário</td>'
        '<option value="operations_admin">Administrador de operações</option>'
        "<p>Este convite oferecia o papel de staff accountant.</p>"
    )

    assert _english_words(_plain(planted)) == ["staff accountant"], (
        f"the scan found {_english_words(_plain(planted))} in a fragment carrying "
        f"exactly one planted English role in its prose; either it misses the words it "
        f"exists to refuse, or it reds on the addresses and stored values that "
        f"legitimately spell them -- and both make every absence above worthless"
    )


def test_the_form_role_maps_are_slices_of_the_single_label_source() -> None:
    """The maps in `apps/accounts/forms.py` must not be a second list of these words.

    Two independently typed lists agree until one of them is edited, and the one that
    is not edited then puts a different word in front of a different reader. The two
    form-boundary maps are therefore read out of `TENANT_ROLE_LABELS`, and this holds
    them to covering it exactly once between them.
    """
    firm_labels = getattr(accounts_forms, "FIRM_ROLE_LABELS", {})
    client_labels = getattr(accounts_forms, "CLIENT_ROLE_LABELS", {})

    overlap = sorted(set(firm_labels) & set(client_labels))
    assert overlap == [], f"a role is named by both maps: {overlap}"
    assert {**firm_labels, **client_labels} == ptbr.TENANT_ROLE_LABELS, (
        "the form-boundary role maps and TENANT_ROLE_LABELS disagree; a role named in "
        "one and not the other falls back to the model's English label on whichever "
        "surface reads the map that lost it"
    )

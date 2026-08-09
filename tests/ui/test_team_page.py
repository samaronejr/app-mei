"""The team screen: who is offered its controls, and what it does on a phone.

Four claims, and they are separable on purpose.

WHICH INVITATIONS. Equipe is a firm-side screen, so its pending table lists only
firm-scoped invitations. A portal invitation belongs to one client's workspace and
must not appear here under a client role that this screen cannot explain.

WHO. The screen is guarded by `require_can("users.create")`, which the published
matrix grants FULL to `owner` and `operations_admin` and NONE to `staff_accountant`.
So the row controls are drawn unconditionally in the template and the *page* is the
thing that is refused: an accountant is answered 403 rather than shown a table with
its action column quietly missing. A 200 with no controls would be indistinguishable
from a render that lost them, which is exactly the failure this module exists to
notice. `tests/ui/test_navigation.py:99-102` pins the same refusal from the
navigation side; it is restated here because the two would otherwise drift apart —
that one is about the link, this one is about the screen.

HOW IT READS ON A PHONE. Both tables carry `.row-stack`, so below the stacking
breakpoint the header row is hidden and every cell has to regrow its column name out
of `data-label`. A cell that omits one becomes a bare value with nothing saying what
it measures, and nothing about that fails on a desktop — which is why it is asserted
against the rendered document rather than left to review. The single exemption is the
empty-state cell, which spans all four columns and so names no one of them; that
exemption is pinned too, or "every cell is labelled" could be satisfied by a page
that quietly stopped labelling and started spanning.

WHAT THE EXPIRY SAYS. `tests/ui/test_layout.py:161-170` already pins that this page
renders a Brasília timestamp. The test below restates it locally and adds the reason:
the countdown beside it is a reading aid and never a replacement, because an invite
is chased against a wall clock and "expira em 6 dias" cannot be compared to one.
"""

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.core.templatetags.ptbr import BRT
from apps.tenants.models import Invite, Membership, TenantRole
from tests.ui.factories import Firm, add_member, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

SLUG: Final = "alpha-equipe"
BARE_SLUG: Final = "beta-equipe"
INVITED: Final = "convidada@example.com"

# The format `ptbr.data_hora` emits, restated rather than imported so that a change to
# the filter is a failure here instead of a silent agreement between the two.
ABSOLUTE: Final = "%d/%m/%Y %H:%M"

# The countdown's own prefix. `locale/` ships no compiled catalogue, so the msgid is
# literally the byte string the document carries.
COUNTDOWN: Final = "expira em"

EMPTY_INVITES: Final = "Nenhum convite pendente."

TABLES_ON_THE_SCREEN: Final = 2

# A table that a `.table-wrap` opens immediately. Matched as one pattern rather than
# counting the two separately, because two wrappers and two tables in the document
# says nothing about whether either table is inside either wrapper.
WRAPPED_TABLE: Final = re.compile(
    r'<div class="table-wrap">\s*<table\b(?P<attrs>[^>]*)>',
    re.IGNORECASE,
)
TABLE_OPEN: Final = re.compile(r"<table\b[^>]*>", re.IGNORECASE)
CELL: Final = re.compile(r"<td\b[^>]*>", re.IGNORECASE)
STACK_CLASSES: Final = ("data-table", "row-stack")
LABEL_ATTRIBUTE: Final = "data-label="
SPAN_ATTRIBUTE: Final = "colspan="

# The two classes `partials/pure/field.html` emits for a visible control, and the
# label/control binding it promises.
FIELD_LABEL: Final = re.compile(r'<label class="field-label" for="(?P<id>[\w-]+)"')
FIELD_INPUT: Final = 'class="field-input"'


@dataclass
class Equipe:
    """A firm with both roles that hold `users.create`, and one invite outstanding."""

    firm: Firm
    ops: User
    invite: Invite

    @property
    def member(self) -> User:
        """The staff accountant: a row on this page, and the role refused it."""
        return self.firm.accountant


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


@pytest.fixture
def equipe() -> Equipe:
    firm = make_firm(SLUG)
    ops = add_member(
        firm.tenant,
        f"ops@{SLUG}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )
    invite, _token = Invite.issue(
        tenant=firm.tenant,
        email=INVITED,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    return Equipe(firm=firm, ops=ops, invite=invite)


def _firm_side(user: User, equipe: Equipe) -> Membership:
    """Return the caller's firm-side row, never a portal identity."""
    return Membership.objects.get(
        user=user,
        tenant=equipe.firm.tenant,
        client__isnull=True,
    )


def _team_page(session: Client, *, who: str) -> str:
    """GET the team screen, refusing to hand back a document that is not it.

    The gate lives here rather than in a sentinel test of its own, because every
    caller below quantifies over this string and `pytest -k` can deselect a sentinel.
    A 403, a diversion to TOTP enrolment or an empty body would satisfy "no control
    is offered", "no cell is unlabelled" and "no table is unwrapped" all at once, for
    a page that never rendered a single row — so the shape of the screen is checked
    before anything is asserted about what is on it.
    """
    response = session.get(reverse("team"))
    assert response.status_code == HTTPStatus.OK, (
        f"{who}: the team screen answered {response.status_code}, so nothing "
        f"asserted below is a statement about a rendered page"
    )
    body = str(response.content.decode())
    tables = TABLE_OPEN.findall(body)
    assert len(tables) == TABLES_ON_THE_SCREEN, (
        f"{who}: the screen drew {len(tables)} tables and this module reads "
        f"{TABLES_ON_THE_SCREEN} — the members list and the pending invites"
    )
    assert INVITED in body, (
        f"{who}: the outstanding invite for {INVITED} is not on the page, so the "
        f"invites table fell to its empty state and every assertion about an "
        f"invite row is a statement about nothing"
    )
    return body


def test_the_team_page_lists_firm_invites_but_not_client_invites() -> None:
    firm = make_firm("escopo-equipe", client_count=1)
    firm_invite, _firm_token = Invite.issue(
        tenant=firm.tenant,
        email="firma-pendente@example.com",
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    client_invite, _client_token = Invite.issue(
        tenant=firm.tenant,
        client=firm.clients[0],
        email="portal-pendente@example.com",
        role=TenantRole.CLIENT_OWNER,
    )

    response = firm.as_owner().get(reverse("team"))
    assert response.status_code == HTTPStatus.OK, response.status_code
    body = str(response.content.decode())

    assert firm_invite.email in body, (
        f"firm-scoped invitation {firm_invite.email} is absent from Equipe"
    )
    assert client_invite.email not in body, (
        f"client-scoped invitation {client_invite.email} is PRESENT in Equipe"
    )


# --------------------------------------------------------------- (a) who may act


def test_the_owner_is_offered_the_control_on_every_row(equipe: Equipe) -> None:
    # Given the owner, for whom `users.create` is FULL
    membership = _firm_side(equipe.member, equipe)

    # When they open the team screen
    body = _team_page(equipe.firm.as_owner(), who="o titular")

    # Then both row controls are drawn, each naming what it acts on
    assert reverse("member-deactivate", args=[membership.pk]) in body
    assert f"Desativar {equipe.member.email}" in body
    assert reverse("invite-revoke", args=[equipe.invite.pk]) in body
    assert f"Revogar convite de {INVITED}" in body


def test_an_operations_admin_is_offered_the_same_controls(equipe: Equipe) -> None:
    """The other role the matrix grants `users.create` FULL to sees the same screen."""
    # Given the operations admin
    membership = _firm_side(equipe.member, equipe)

    # When they open the team screen
    body = _team_page(equipe.firm.sign_in(equipe.ops), who="o administrador")

    # Then it offers exactly what it offers the owner — the page is guarded by the
    # capability, not by the role, so a check written against `owner` would be wrong
    assert reverse("member-deactivate", args=[membership.pk]) in body
    assert reverse("invite-revoke", args=[equipe.invite.pk]) in body


def test_a_staff_accountant_is_refused_the_screen_rather_than_shown_it_empty(
    equipe: Equipe,
) -> None:
    """Hiding the controls would be cosmetic; refusing the page is the control."""
    # Given the staff accountant, for whom `users.create` is NONE
    # When they ask for the team screen
    response = equipe.firm.as_accountant().get(reverse("team"))

    # Then they are refused it outright — a 200 carrying a table with its action
    # column stripped is indistinguishable from a render that lost the column
    assert response.status_code == HTTPStatus.FORBIDDEN, response.status_code


# ------------------------------------------------------- (b) how it reads on a phone


def test_both_tables_are_wrapped_and_stack_below_the_breakpoint(
    equipe: Equipe,
) -> None:
    # Given the rendered screen
    body = _team_page(equipe.firm.as_owner(), who="o titular")

    # When each table is read together with what encloses it
    wrapped = WRAPPED_TABLE.findall(body)

    # Then every table on the page is inside a scroll wrapper and carries both
    # classes: the wrapper keeps a wide table from pushing the page sideways, and
    # `.row-stack` is what turns each row into a labelled block on a phone
    assert len(wrapped) == TABLES_ON_THE_SCREEN, (
        f"{len(wrapped)} of {TABLES_ON_THE_SCREEN} tables are opened by a "
        f"`.table-wrap`; an unwrapped one overflows the viewport on a phone"
    )
    for attributes in wrapped:
        for expected in STACK_CLASSES:
            assert expected in attributes, f"missing {expected}: <table{attributes}>"


def test_every_cell_names_the_column_it_lost_when_the_row_stacked(
    equipe: Equipe,
) -> None:
    # Given a screen with rows in both tables — the gate in `_team_page` proves the
    # invites table did not fall to its empty state
    body = _team_page(equipe.firm.as_owner(), who="o titular")

    # When every cell is read
    cells = CELL.findall(body)
    spanning = [tag for tag in cells if SPAN_ATTRIBUTE in tag]
    unlabelled = [tag for tag in cells if LABEL_ATTRIBUTE not in tag]

    # Then no cell spans the row, because both tables have data…
    assert spanning == [], (
        f"a row-spanning cell was drawn on a page whose tables both have rows, so "
        f"the exemption below is being claimed by a cell that is not an empty "
        f"state: {spanning}"
    )
    # …and every one of them carries its column name, or it renders on a phone as a
    # value with nothing saying what it measures
    assert unlabelled == [], (
        f"{len(unlabelled)} of {len(cells)} cells carry no {LABEL_ATTRIBUTE!r}: "
        f"{unlabelled}"
    )


def test_the_empty_state_spans_the_columns_rather_than_naming_one(
    equipe: Equipe,
) -> None:
    """The one cell exempt from the rule above, and the reason it is the only one."""
    # Given a firm with nobody invited yet
    firm = make_firm(BARE_SLUG)
    response = firm.as_owner().get(reverse("team"))
    assert response.status_code == HTTPStatus.OK, response.status_code
    body = str(response.content.decode())
    assert EMPTY_INVITES in body, (
        "the invites table did not fall to its empty state, so this test is not "
        "reading the cell it names"
    )

    # When the spanning cells are read
    spanning = [tag for tag in CELL.findall(body) if SPAN_ATTRIBUTE in tag]

    # Then the empty state is the only one, and it claims no column name — it covers
    # all four, so any label it carried would be a lie about three of them
    assert len(spanning) == 1, spanning
    assert LABEL_ATTRIBUTE not in spanning[0], spanning[0]


# --------------------------------------------------------- (c) what the expiry says


def test_the_expiry_is_an_absolute_brasilia_timestamp_with_the_countdown_beside_it(
    equipe: Equipe,
) -> None:
    # Given a pending invite
    body = _team_page(equipe.firm.as_owner(), who="o titular")

    # When the expiry cell is read
    absolute = equipe.invite.expires_at.astimezone(BRT).strftime(ABSOLUTE)

    # Then the wall-clock time an owner can act on is on the page…
    assert absolute in body, (
        f"the invite's expiry is not rendered as {ABSOLUTE} in Brasília time, so "
        f"the page carries no time anybody can compare against a calendar"
    )
    # …carried by a <time>, so it is machine-readable as well as legible…
    assert f'<time datetime="{equipe.invite.expires_at.isoformat()}"' in body
    # …and the countdown sits beside it rather than in place of it
    assert COUNTDOWN in body, f"the countdown reading aid is missing: {COUNTDOWN!r}"


# ------------------------------------------------- (d) the invite form's own markup


def test_the_invite_form_is_rendered_through_the_shared_field_partial(
    equipe: Equipe,
) -> None:
    """Written out by hand, the control would ship unstyled with every guard green."""
    # Given the rendered screen
    body = _team_page(equipe.firm.as_owner(), who="o titular")

    # When the form's first label is read
    label = FIELD_LABEL.search(body)

    # Then the partial drew it, bound to a control that exists — a label pointing at
    # no id is a label a screen reader announces for nothing
    assert label is not None, "no `.field-label` on the page: the partial did not run"
    assert f'id="{label.group("id")}"' in body, label.group(0)
    assert FIELD_INPUT in body, "the partial drew no styled control"

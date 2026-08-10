"""Pending portal invitations, listed and withdrawn where they were issued.

The client workspace is where an accountant decides a client should see their own
obligations, and since `cd4de7e` it is the only place a portal invitation is legible
at all: Equipe now filters `client__isnull=True`, so a client-scoped row appears on no
firm-side screen. Issuing without listing would leave a live credential nobody can see
and nobody can stand down — which is the gap this module closes and pins.

Three claims, separable on purpose.

WHICH ROWS. Pending means `accepted_at__isnull=True` AND `revoked_at__isnull=True`,
and it means THIS client. Both halves are asserted positively and negatively, because
a list that has stopped filtering and a list that has stopped rendering look identical
from the row that is correctly absent.

**Isolation is two-dimensional, and one dimension does not imply the other.** A page
that filtered on tenant alone would pass every cross-tenant assertion ever written and
still show one client's invited address on a sibling client's page — same firm, same
tenant, wrong books. So the leak is chased along both axes: a SIBLING client inside the
SAME tenant, and ANOTHER tenant in which the acting account also holds a firm-side
membership. The second is not the usual stranger-tenant test: `Invite.objects.for_user`
narrows to the firms the caller belongs to, so a caller who belongs to only one firm is
scoped correctly by the manager no matter what the view forgets. Giving the actor a
real second membership is what removes that safety net and makes the view's own
`tenant_id` filter the thing under test.

WHO SEES IT. The invited address is a person's email and the revoke control destroys a
credential; both are `users.create` business. The detail screen itself is guarded by
`clients.view_assigned`, which the published matrix grants to a staff accountant — so
an assigned accountant reaches this page at 200 and must find neither. That is why the
gate is asserted from inside a rendered 200 rather than against a refusal: a 403 would
satisfy "the address is absent" for a page that was never drawn.

WHAT THE CONTROL IS. The existing `invite-revoke` endpoint, reached by the same POST
`templates/accounts/invite_confirm.html:27` already uses. No route, view, service,
model or migration is added here; the row simply offers the verb that already exists.
"""

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.clients import views as clients_views
from apps.clients.models import ClientCompany
from apps.core.templatetags.ptbr import data_hora, papel
from apps.tenants.models import Invite, Membership, TenantRole
from tests.ui.factories import Firm, add_client, add_member, assign, make_firm

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db(transaction=True)

SLUG: Final = "alpha-convites"
SIBLING_SLUG: Final = "gama-convites"

# Distinct enough that `not in body` is a statement about this address and not about a
# substring of somebody else's. Every one of them is a full mailbox, because asserting
# absence on a fragment is how a leak survives its own regression test.
INVITED: Final = "portal-pendente@example.com"
SIBLING_INVITED: Final = "vizinho-pendente@example.com"
OTHER_TENANT_INVITED: Final = "outra-firma-pendente@example.com"
ACCEPTED: Final = "portal-aceito@example.com"
REVOKED: Final = "portal-revogado@example.com"

# The issuance form this section already carried, plus the withdrawal form the row now
# adds. Counted rather than located, because `{% csrf_token %}` renders the same hidden
# input in both and a count is the one assertion a form that lost its token fails.
PROTECTED_FORMS: Final = 2


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", "localhost", ".localhost"]


def _detail(session: Client, company: ClientCompany, *, who: str) -> str:
    """GET one client's workspace, refusing to hand back a document that is not it.

    The two gates live here rather than in a sentinel of their own, for the reason
    `tests/ui/test_clients_pages.py` gives about the same helper: `pytest -k` can
    deselect a standalone sentinel, and every absence assertion below is satisfied
    outright by a 403, by a diversion to TOTP enrolment, or by an empty body.
    """
    response: _MonkeyPatchedWSGIResponse = session.get(
        reverse("client-detail", args=[company.pk]),
    )
    assert response.status_code == HTTPStatus.OK, (
        f"{who}: the client workspace answered {response.status_code} for "
        f"{company.legal_name}, so nothing asserted below is a statement about a "
        f"rendered page"
    )
    body = str(response.content.decode())
    assert company.legal_name in body, (
        f"{who}: the page answered 200 without naming {company.legal_name}, so this "
        f"is some other client's document"
    )
    return body


def _pending(firm: Firm, company: ClientCompany, email: str) -> Invite:
    """Issue one outstanding portal invitation for a client."""
    invite, _token = Invite.issue(
        tenant=firm.tenant,
        client=company,
        email=email,
        role=TenantRole.CLIENT_OWNER,
    )
    return invite


# --------------------------------------------------------- (1) the rows that belong


def test_the_workspace_lists_this_client_s_outstanding_invitation() -> None:
    """The address, the role in Portuguese, the expiry, and the way to withdraw it."""
    firm = make_firm(SLUG, client_count=1)
    company = firm.clients[0]
    invite = _pending(firm, company, INVITED)

    body = _detail(firm.as_owner(), company, who="o titular")

    assert INVITED in body, (
        f"the outstanding portal invitation for {INVITED} is absent from "
        f"{company.legal_name}'s workspace, so the firm has issued a live credential "
        f"that no screen in the product shows"
    )
    assert papel(TenantRole.CLIENT_OWNER) in body, (
        f"the row does not name the role in Portuguese; "
        f"{papel(TenantRole.CLIENT_OWNER)} is what the |papel filter emits"
    )
    assert data_hora(invite.expires_at) in body, (
        "the row carries no absolute expiry, so nobody can compare the invitation's "
        "deadline against a wall clock"
    )
    assert reverse("invite-revoke", args=[invite.pk]) in body, (
        f"the workspace offers no way to withdraw the invitation for {INVITED}"
    )
    assert f'aria-label="Revogar convite de {INVITED}"' in body, (
        f"the revoke control does not name {INVITED} in its accessible name. The "
        f"visible word is only the verb, so without this the actions read out of "
        f"table context are N identical 'Revogar' buttons"
    )


def test_an_accepted_or_revoked_invitation_is_not_listed_as_pending() -> None:
    """Only live links appear; a dead one offering Revogar is a lie about access."""
    firm = make_firm("historico-convites", client_count=1)
    company = firm.clients[0]

    live = _pending(firm, company, INVITED)
    accepted = _pending(firm, company, ACCEPTED)
    revoked = _pending(firm, company, REVOKED)
    Invite.objects.filter(pk=accepted.pk).update(accepted_at=live.expires_at)
    Invite.objects.filter(pk=revoked.pk).update(revoked_at=live.expires_at)

    body = _detail(firm.as_owner(), company, who="o titular")

    assert INVITED in body, "the live invitation fell out of the list too"
    assert ACCEPTED not in body, (
        f"{ACCEPTED} was already accepted and is still listed as pending, so the "
        f"page offers Revogar for something there is nothing left to revoke"
    )
    assert REVOKED not in body, (
        f"{REVOKED} was already revoked and is still listed as pending, so the page "
        f"tells the firm a dead link is live"
    )


# ------------------------------------- (2) isolation, along BOTH of its dimensions


def test_a_sibling_client_in_the_same_tenant_does_not_leak_its_invitation() -> None:
    """Same firm, same tenant, different books — and the tenant filter cannot tell.

    This is the dimension a one-dimensional isolation suite misses entirely. Both
    clients belong to the caller's own firm, so `Invite.objects.for_user` admits both
    rows and every tenant assertion in the product passes while one client's page
    names the other client's invitee.
    """
    firm = make_firm(SIBLING_SLUG, client_count=1)
    subject = firm.clients[0]
    sibling = add_client(firm, legal_name="Gama Vizinha MEI", base="119887766001")

    _pending(firm, subject, INVITED)
    intruder = _pending(firm, sibling, SIBLING_INVITED)

    body = _detail(firm.as_owner(), subject, who="o titular")

    assert INVITED in body, (
        "this client's own invitation is absent, so the assertion below would pass "
        "against a list that renders nothing at all"
    )
    assert SIBLING_INVITED not in body, (
        f"{SIBLING_INVITED} was invited to {sibling.legal_name}'s portal and appears "
        f"on {subject.legal_name}'s page: one client's workspace is disclosing "
        f"another client's invitee inside the same firm"
    )
    assert reverse("invite-revoke", args=[intruder.pk]) not in body, (
        f"{subject.legal_name}'s page offers a control that withdraws "
        f"{sibling.legal_name}'s invitation"
    )


def test_another_tenant_the_actor_also_belongs_to_does_not_leak_its_invitation() -> (
    None
):
    """A second firm-side membership removes the manager's safety net.

    `Invite.objects.for_user` narrows to the firms the caller belongs to, so a caller
    with one membership is scoped correctly by the manager even if the view filters on
    nothing. Enrolling the same account in a second firm is what makes the view's own
    `tenant_id` filter load-bearing and therefore testable.
    """
    firm = make_firm("primeira-firma", client_count=1)
    other = make_firm("segunda-firma", client_count=1)
    subject = firm.clients[0]

    # The same human, an owner in both books. Firm-side, so `client` stays NULL and
    # `membership_role_matches_client_scope` is satisfied.
    Membership.objects.create(
        user=firm.owner,
        tenant=other.tenant,
        role=TenantRole.OWNER,
        client=None,
    )

    _pending(firm, subject, INVITED)
    foreign = _pending(other, other.clients[0], OTHER_TENANT_INVITED)

    body = _detail(firm.as_owner(), subject, who="o titular de duas firmas")

    assert INVITED in body, (
        "this client's own invitation is absent, so the assertion below would pass "
        "against a list that renders nothing at all"
    )
    assert OTHER_TENANT_INVITED not in body, (
        f"{OTHER_TENANT_INVITED} belongs to {other.tenant.slug} and appears on a page "
        f"served by {firm.tenant.slug}: the list is scoped by membership rather than "
        f"by the tenant this request resolved"
    )
    assert reverse("invite-revoke", args=[foreign.pk]) not in body, (
        f"{firm.tenant.slug}'s page offers a control that withdraws "
        f"{other.tenant.slug}'s invitation"
    )


# ----------------------------------------------- (3) who is shown it, and with what


def test_the_pending_list_is_filtered_on_both_the_tenant_and_the_client() -> None:
    """Read against the source, because no rendered page can tell these two apart.

    Every other assertion in this module is behavioural, and this one cannot be. The
    client is resolved through `visible_clients(user, tenant_id=...)` before the list
    is built, so `client_id` already pins a row belonging to exactly one firm — which
    makes the `tenant_id` filter beside it genuinely redundant. Deleting it reds
    nothing: that was measured, not assumed, by mutating it out and watching all eight
    tests here stay green.

    A redundant guard no test can red is a guard the next reader deletes as dead code,
    which is precisely how defence in depth erodes — one obviously-unnecessary line at
    a time, each removal green. So the belt is pinned here and the braces are pinned by
    `test_a_sibling_client_in_the_same_tenant_does_not_leak_its_invitation` above.
    """
    source = Path(clients_views.__file__).read_text(encoding="utf-8")
    queryset = source.split('"pending_portal_invites"', maxsplit=1)[-1].split(
        ".order_by(",
        maxsplit=1,
    )[0]

    assert "Invite.objects.for_user(request.user)" in queryset, (
        "the pending list does not go through the for_user manager, which is the only "
        "isolation tenants_invite has — it is in NON_TENANT_TABLES, so the database "
        "enforces nothing on it"
    )
    assert "tenant_id=tenant_id," in queryset, (
        "the pending list no longer filters on tenant_id. Nothing behavioural catches "
        "this today, which is exactly why it is asserted here: it is the filter that "
        "still holds if client_id or the manager is ever wrong"
    )
    assert "client_id=client.pk," in queryset, (
        "the pending list no longer filters on client_id, so one client's workspace "
        "will name another client's invitee inside the same firm"
    )


def test_a_role_without_users_create_sees_neither_address_nor_control() -> None:
    """Asserted from inside a rendered 200, never from a refusal.

    A staff accountant holds `clients.view_assigned`, so with an assignment they open
    this page successfully — which is exactly the account that would be handed the
    invited address by a list drawn outside the gate. Refusing the page instead would
    make both assertions below true for a document that was never built.
    """
    firm = make_firm("sem-permissao", client_count=1)
    company = firm.clients[0]
    assign(firm, company, firm.accountant)
    invite = _pending(firm, company, INVITED)

    body = _detail(firm.as_accountant(), company, who="a contadora")

    assert INVITED not in body, (
        f"{INVITED} is disclosed to {TenantRole.STAFF_ACCOUNTANT}, a role the matrix "
        f"grants no users.create — so the list sits outside the authorization gate"
    )
    assert reverse("invite-revoke", args=[invite.pk]) not in body, (
        f"{TenantRole.STAFF_ACCOUNTANT} is offered a control that withdraws an "
        f"invitation, which only users.create may do"
    )


def test_an_operations_admin_is_offered_the_same_rows_as_the_owner() -> None:
    """`users.create` is the gate, not ownership."""
    firm = make_firm("ops-convites", client_count=1)
    company = firm.clients[0]
    ops = add_member(
        firm.tenant,
        "ops@ops-convites.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )
    invite = _pending(firm, company, INVITED)

    body = _detail(firm.sign_in(ops), company, who="a administradora")

    assert INVITED in body, (
        f"{TenantRole.OPERATIONS_ADMIN} holds users.create and is not shown the "
        f"pending invitation"
    )
    assert reverse("invite-revoke", args=[invite.pk]) in body, (
        f"{TenantRole.OPERATIONS_ADMIN} holds users.create and is offered no way to "
        f"withdraw the invitation"
    )


def test_the_revoke_control_posts_to_the_existing_endpoint_with_a_csrf_token() -> None:
    """Reuse, and the proof of it: the verb is the one already shipped.

    The withdrawal is a state change, so it is a POST and not a link — and the token
    is asserted because a form that omits it is answered 403 by
    `CsrfViewMiddleware` and the control is decorative.
    """
    firm = make_firm("posta-convites", client_count=1)
    company = firm.clients[0]
    invite = _pending(firm, company, INVITED)
    action = reverse("invite-revoke", args=[invite.pk])

    body = _detail(firm.as_owner(), company, who="o titular")

    assert f'method="post" action="{action}"' in body, (
        f"the revoke control does not POST to {action}; a GET cannot carry a state "
        f"change and the shipped endpoint is what this row must reuse"
    )
    assert body.count("csrfmiddlewaretoken") >= PROTECTED_FORMS, (
        f"the page carries fewer than {PROTECTED_FORMS} CSRF tokens, so either the "
        f"issuance form or the new revoke form is unprotected"
    )


def test_the_control_actually_withdraws_the_invitation_it_names() -> None:
    """End to end, because a rendered form proves nothing about what it does."""
    firm = make_firm("revoga-de-fato", client_count=1)
    company = firm.clients[0]
    invite = _pending(firm, company, INVITED)

    session = firm.as_owner()
    before = _detail(session, company, who="o titular")
    assert INVITED in before, (
        f"{INVITED} is not listed before the withdrawal, so the disappearance "
        f"asserted at the end of this test is not a disappearance"
    )
    response: _MonkeyPatchedWSGIResponse = session.post(
        reverse("invite-revoke", args=[invite.pk]),
    )

    assert response.status_code == HTTPStatus.FOUND, (
        f"the shipped endpoint answered {response.status_code} to the POST this page "
        f"now renders"
    )
    invite.refresh_from_db()
    assert invite.revoked_at is not None, (
        f"the invitation for {INVITED} is still live after the control on its own "
        f"row was submitted"
    )

    # The redirect is followed rather than dropped, and it has to be. `revoke_invite`'s
    # view queues `messages.success` NAMING THE ADDRESS, and Django's test client does
    # not follow a 302 on its own — so an unconsumed flash is rendered by whatever page
    # is fetched next, and the absence assertion below would fail on the confirmation
    # message instead of on the list it is about.
    session.get(response.headers["Location"])

    body = _detail(session, company, who="o titular")
    assert INVITED not in body, (
        f"{INVITED} was withdrawn and is still listed as pending"
    )

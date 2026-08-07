"""A portal seat is issued by the firm, redeemed on the portal host, scoped to a client.

The firm-side invitation already has a suite next door
(`tests/accounts/test_invites.py`) and every property it pins holds here too: the token
is a bearer credential, so possession of the link is not sufficient and the accepting
session must prove control of the invited mailbox. What is NEW is the client dimension,
and it is where every interesting failure lives:

* the seat must land as a SECOND membership beside a firm-side one, never in place of it
  — the firm door reads `client__isnull=True` and the portal door reads
  `client__isnull=False`, so converting the row would revoke an accountant's access to
  their own firm as the side effect of giving them a portal login;
* the acceptance route runs on a host whose middleware would otherwise refuse it twice
  over, once for having no client membership yet and once for the database role;
* and a token that resolves is not automatically a token that belongs at THIS firm's
  door.

Every refusal below is exercised at the layer that actually refuses. The domain cases
call the domain, the HTTP cases drive a real request through the real middleware chain,
and the rate-limit case reads its verdict off the middleware rather than off a setting.
"""

import re
from datetime import timedelta
from http import HTTPStatus
from typing import Final, cast

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.http.response import HttpResponseBase
from django.test import Client
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import (
    InviteEmailMismatchError,
    InviteExpiredError,
    InviteHostMismatchError,
    InviteRevokedError,
    InviteScopeMismatchError,
    accept_invite,
    assert_portal_invite,
    issue_client_invite,
    issue_invite,
    resolve_invite,
    revoke_invite,
)
from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.portal.middleware import INVITE_PREFIX, DispatchableHttpRequest
from apps.security.ratelimit import is_public_post_endpoint
from apps.tenants.models import (
    CLIENT_ROLES,
    PORTAL_INVITE_ROLES,
    Invite,
    Membership,
    Tenant,
    TenantRole,
    client_role_choices,
)
from tests.support import enrol_totp, pin_rate_limit_window

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105
INVITED = "dona@padaria.example"
ACME_PORTAL = "acme-portal.localhost"
BETA_PORTAL = "beta-portal.localhost"
ACME_FIRM = "acme.localhost"
FIRM_URLCONF: Final = "config.urls"
PORTAL_URLCONF: Final = "apps.portal.urls"

# The shipped `RATELIMIT_LOGIN_EMAIL` budget. The sixth attempt is the one this suite
# needs refused.
LOGIN_ATTEMPTS_ALLOWED: Final = 5


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


class Firm:
    """One firm, two of its MEI clients, and the owner who administers its roster."""

    def __init__(
        self,
        tenant: Tenant,
        padaria: ClientCompany,
        oficina: ClientCompany,
        owner: User,
    ) -> None:
        self.tenant = tenant
        self.padaria = padaria
        self.oficina = oficina
        self.owner = owner


def _client_company(tenant: Tenant, name: str, cnpj: str) -> ClientCompany:
    with tenant_context(tenant.id):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        return ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name=name,
            cnpj=cnpj,
            is_mei=True,
        )


def _firm(
    slug: str = "acme", cnpjs: tuple[str, str] = ("11222333000181", "11444777000161")
) -> Firm:
    tenant = Tenant.objects.create(name=slug.title(), slug=slug)
    padaria = _client_company(tenant, f"PADARIA {slug.upper()}", cnpjs[0])
    oficina = _client_company(tenant, f"OFICINA {slug.upper()}", cnpjs[1])
    owner = User.objects.create_user(email=f"dono@{slug}.example", password=PASSWORD)
    Membership.objects.create(user=owner, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(owner)
    _verified(owner)
    return Firm(tenant, padaria, oficina, owner)


@pytest.fixture
def firm() -> Firm:
    return _firm()


def _verified(user: User, email: str | None = None) -> EmailAddress:
    return EmailAddress.objects.create(
        user=user,
        email=email or user.email,
        verified=True,
        primary=True,
    )


def _issue(firm: Firm, client: ClientCompany | None = None) -> tuple[Invite, str]:
    """Issue a portal invitation through the domain, and refuse a vacuous fixture.

    The assertions live INSIDE this helper rather than in one case that `pytest -k`
    could deselect. A fixture that quietly stopped producing client-scoped invitations
    would leave every case below exercising the FIRM-side flow under a portal-shaped
    name, and all of them would still pass.
    """
    target = client or firm.padaria
    invite, raw_token = issue_client_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        client=target,
        email=INVITED,
        role=TenantRole.CLIENT_OWNER,
    )
    assert invite.client_id == target.pk, (
        "the invitation is not client-scoped, so every case in this module would be "
        "exercising the firm-side flow under a portal-shaped name"
    )
    assert invite.role in CLIENT_ROLES
    assert raw_token, "no raw token was returned, so nothing can be redeemed"
    assert invite.token != raw_token, "the raw token was stored instead of its digest"
    return invite, raw_token


def _accept_path(raw_token: str) -> str:
    """Return the portal acceptance path, proving it resolves and stays unconfined.

    Two non-vacuity gates, both here rather than in a case. The route must exist on the
    PORTAL urlconf — `reverse` against the wrong tree raises rather than answering — and
    the path must sit under `PortalMiddleware.INVITE_PREFIX`, because that prefix is the
    only thing keeping the portal role and the client-membership gate off this flow. A
    path that drifts out of it resolves perfectly and 500s every acceptance.
    """
    path = reverse("portal-invite-accept", args=[raw_token], urlconf=PORTAL_URLCONF)
    assert path.startswith(INVITE_PREFIX), (
        f"{path} is outside {INVITE_PREFIX}, so PortalMiddleware would confine it"
    )
    try:
        resolve(path, urlconf=PORTAL_URLCONF)
    except Resolver404 as missing:  # pragma: no cover - defensive
        pytest.fail(f"{path} does not resolve on the portal urlconf: {missing}")
    return path


def _register(
    raw_token: str,
    host: str = ACME_PORTAL,
    ip: str = "203.0.113.7",
) -> HttpResponseBase:
    return Client().post(
        _accept_path(raw_token),
        {"full_name": "Dona Maria", "password1": PASSWORD, "password2": PASSWORD},
        headers={"host": host},
        REMOTE_ADDR=ip,
    )


def _post_issue(firm: Firm, client_pk: object) -> HttpResponseBase:
    session = Client()
    session.force_login(firm.owner)
    return session.post(
        reverse("portal-invite-issue", args=[client_pk], urlconf=FIRM_URLCONF),
        {"email": INVITED, "role": TenantRole.CLIENT_OWNER},
        headers={"host": ACME_FIRM},
    )


def _token_from_outbox() -> str:
    body = str(mail.outbox[-1].body)
    match = re.search(r"/convites/aceitar/([^/\s]+)/", body)
    assert match is not None, f"no acceptance link in the message body: {body!r}"
    return match.group(1)


def _platform_events(action: str, invite: Invite) -> list[PlatformEvent]:
    """Return this invitation's events of one kind, refusing an empty table.

    The emptiness check is inside the helper for the same reason the fixture's is: a
    case that filtered an empty table would satisfy its assertions while proving the
    audit trail records nothing at all.
    """
    assert PlatformEvent.objects.exists(), (
        "no platform events were written at all, so filtering them proves nothing"
    )
    return list(
        PlatformEvent.objects.filter(
            action=action,
            metadata__invite_id=str(invite.pk),
        ),
    )


# ------------------------------------------------------------------------- the choices


def test_the_portal_choices_agree_with_the_check_constraint() -> None:
    # Given the roles a portal invitation may offer
    offered = {value for value, _label in client_role_choices()}

    # Then they are exactly the arm of `invite_role_is_firm_side` that admits a client.
    # The tuple is enumerated rather than filtered out of TenantRole, so this is what
    # stops it drifting away from the constraint it exists to satisfy.
    assert offered == {str(role) for role in CLIENT_ROLES}
    assert {str(role) for role in PORTAL_INVITE_ROLES} == offered


# ------------------------------------------------------------------------------- issue


def test_an_owner_issues_a_client_scoped_invitation_and_it_is_mailed(
    firm: Firm,
) -> None:
    # Given a firm owner and one of their MEI clients
    # When they invite that client's owner into the portal
    response = _post_issue(firm, firm.padaria.pk)

    # Then exactly one invitation exists, scoped to that client, and one message went
    # out
    assert response.status_code == HTTPStatus.FOUND
    invite = Invite.objects.get(email=INVITED)
    assert invite.client_id == firm.padaria.pk
    assert invite.role == TenantRole.CLIENT_OWNER
    assert len(mail.outbox) == 1
    assert INVITED in mail.outbox[0].to


def test_the_mailed_link_points_at_that_firm_s_portal_host(firm: Firm) -> None:
    # Given an issued portal invitation
    _post_issue(firm, firm.padaria.pk)

    # Then the address in it is the portal host, not the firm subdomain and not the
    # platform host. A link built against either would land the invitee somewhere the
    # acceptance route is not mounted.
    body = mail.outbox[-1].body
    assert f"{firm.tenant.slug}-portal." in body
    assert _accept_path(_token_from_outbox()) in body


def test_the_raw_token_is_never_persisted(firm: Firm) -> None:
    # Given an issued portal invitation
    _invite, raw_token = _issue(firm)

    # When every stored column value is scanned
    stored = [str(value) for row in Invite.objects.values() for value in row.values()]

    # Then the bearer value appears nowhere — only its digest is held
    assert raw_token not in stored


def test_the_token_carries_the_same_lifecycle_as_a_firm_invitation(firm: Firm) -> None:
    # Given an issued portal invitation
    invite, raw_token = _issue(firm)

    # Then it is the same credential the firm flow mints: a urlsafe-32 secret stored as
    # its SHA-256 digest, dated seven days out.
    assert invite.token == Invite.hash_token(raw_token)
    assert len(invite.token) == 64
    assert len(raw_token) >= 43
    assert timedelta(days=6) < invite.expires_at - timezone.now() <= timedelta(days=7)


def test_the_issue_event_records_which_client_the_seat_is_in(firm: Firm) -> None:
    # Given an issued portal invitation
    invite, _raw_token = _issue(firm)

    # Then the audit row names the client, not merely the firm. Without it an
    # investigation cannot tell whose books this link opened.
    issued = _platform_events(AuditAction.INVITE_ISSUED, invite)
    assert len(issued) == 1
    assert issued[0].metadata["client_id"] == str(firm.padaria.pk)
    assert issued[0].tenant_id == firm.tenant.pk
    assert issued[0].subject == INVITED


def test_a_firm_invitation_records_a_null_client_rather_than_omitting_the_key(
    firm: Firm,
) -> None:
    # Given a FIRM-side invitation issued through the same audit path
    invite, _raw = issue_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        email="novo@acme.example",
        role=TenantRole.STAFF_ACCOUNTANT,
    )

    # Then the key is present and null. A missing key cannot be told apart from an event
    # written before the portal existed, which is the distinction this payload is for.
    issued = _platform_events(AuditAction.INVITE_ISSUED, invite)
    assert len(issued) == 1
    assert "client_id" in issued[0].metadata
    assert issued[0].metadata["client_id"] is None


def test_the_withdrawal_event_records_the_client_scope_too(firm: Firm) -> None:
    # Given a portal invitation the firm stands down
    invite, _raw_token = _issue(firm)
    revoke_invite(actor=firm.owner, invite=invite)

    # Then the third leg of its life names the client as the other two do
    revoked = _platform_events(AuditAction.INVITE_REVOKED, invite)
    assert len(revoked) == 1
    assert revoked[0].metadata["client_id"] == str(firm.padaria.pk)


# -------------------------------------------------------------------------- acceptance


def test_a_new_invitee_registers_on_the_portal_host_and_gets_one_client_seat(
    firm: Firm,
) -> None:
    # Given a portal invitation for somebody with no account yet
    invite, raw_token = _issue(firm)

    # When they set a password at the portal acceptance route
    response = _register(raw_token)

    # Then they hold exactly one membership, and it names the client the invite named
    assert response.status_code == HTTPStatus.FOUND
    user = User.objects.get(email=INVITED)
    seats = Membership.objects.filter(user=user, client__isnull=False)
    assert seats.count() == 1
    assert seats.get().client_id == firm.padaria.pk
    assert seats.get().role == TenantRole.CLIENT_OWNER
    assert seats.get().is_active
    # …and no firm-side row was created alongside it, which would be a portal login that
    # also opens the firm's whole portfolio.
    assert not Membership.objects.filter(user=user, client__isnull=True).exists()
    assert Invite.objects.get(pk=invite.pk).accepted_at is not None


def test_acceptance_lands_the_invitee_on_their_own_portal_home(firm: Firm) -> None:
    # Given a portal invitation redeemed by a new account
    _invite, raw_token = _issue(firm)

    # When it is accepted
    response = _register(raw_token)

    # Then the redirect is the portal landing page rather than the firm dashboard
    assert response["Location"] == reverse("portal-home", urlconf=PORTAL_URLCONF)


def test_the_acceptance_event_records_the_client_scope(firm: Firm) -> None:
    # Given a redeemed portal invitation
    invite, raw_token = _issue(firm)
    _register(raw_token)

    # Then the acceptance is recorded against the same client the issue was
    accepted = _platform_events(AuditAction.INVITE_ACCEPTED, invite)
    assert len(accepted) == 1
    assert accepted[0].metadata["client_id"] == str(firm.padaria.pk)
    assert accepted[0].metadata["role"] == TenantRole.CLIENT_OWNER


def test_a_firm_member_accepting_a_client_invite_gains_a_second_membership(
    firm: Firm,
) -> None:
    # Given an accountant who already works at this firm
    accountant = User.objects.create_user(email=INVITED, password=PASSWORD)
    firm_seat = Membership.objects.create(
        user=accountant,
        tenant=firm.tenant,
        role=TenantRole.STAFF_ACCOUNTANT,
        client=None,
    )
    _verified(accountant)
    enrol_totp(accountant)
    invite, raw_token = _issue(firm)
    session = Client()
    session.force_login(accountant)

    # When they accept a portal seat in one of the firm's clients
    response = session.post(_accept_path(raw_token), {}, headers={"host": ACME_PORTAL})

    # Then they hold TWO rows, not one converted row
    assert response.status_code == HTTPStatus.FOUND
    assert Membership.objects.filter(user=accountant, tenant=firm.tenant).count() == 2

    # The firm row is untouched: same primary key, same role, still firm-side and still
    # active. Mutating it would have revoked this accountant's access to their own firm
    # as the side effect of handing them a portal login.
    firm_seat.refresh_from_db()
    assert firm_seat.client_id is None
    assert firm_seat.role == TenantRole.STAFF_ACCOUNTANT
    assert firm_seat.is_active

    # And the new row is the client seat, a DIFFERENT row
    portal_seat = Membership.objects.get(user=accountant, client__isnull=False)
    assert portal_seat.pk != firm_seat.pk
    assert portal_seat.client_id == firm.padaria.pk
    assert portal_seat.role == TenantRole.CLIENT_OWNER
    assert Invite.objects.get(pk=invite.pk).accepted_at is not None


def test_a_second_client_seat_is_added_beside_the_first_not_moved(firm: Firm) -> None:
    # Given somebody who already holds a portal seat in one client of this firm
    _first, first_token = _issue(firm, client=firm.padaria)
    _register(first_token)
    invitee = User.objects.get(email=INVITED)
    # Enrolled deliberately. The seat they just accepted is an active Membership, so
    # `requires_mfa` is now true of them and every later request — the acceptance route
    # included, since `/convites/` is no more exempt from the second factor than the
    # firm-side acceptance route is — diverts to enrolment until they carry one.
    # Skipping this asserts against that redirect rather than against the second seat.
    enrol_totp(invitee)

    # When the firm invites the same person into a SECOND client
    _second, second_token = _issue(firm, client=firm.oficina)
    session = Client()
    session.force_login(invitee)
    response = session.post(
        _accept_path(second_token),
        {},
        headers={"host": ACME_PORTAL},
    )

    # Then both seats exist. `client` is in the get-or-create LOOKUP, so the second
    # acceptance creates a row rather than matching and rewriting the first.
    assert response.status_code == HTTPStatus.FOUND
    seats = Membership.objects.filter(user=invitee, client__isnull=False)
    assert seats.count() == 2
    assert {seat.client_id for seat in seats} == {firm.padaria.pk, firm.oficina.pk}


def test_an_anonymous_invitee_reaches_the_page_on_the_portal_host(firm: Firm) -> None:
    # Given a portal invitation and no session at all
    _invite, raw_token = _issue(firm)

    # When the link is opened
    response = Client().get(_accept_path(raw_token), headers={"host": ACME_PORTAL})

    # Then the page is served — the unconfined prefix is what makes this a 200 rather
    # than the `permission denied` the portal role would produce
    assert response.status_code == HTTPStatus.OK
    assert INVITED in response.content.decode()


def test_the_acceptance_route_is_not_mounted_on_the_firm_url_tree() -> None:
    # Given the firm-side tree
    # Then the name is absent from it. Served there, TenantMiddleware would refuse every
    # invitee — they hold no firm-side membership, which is the whole reason this route
    # lives on the portal.
    with pytest.raises(NoReverseMatch):
        reverse("portal-invite-accept", args=["qualquer"], urlconf=FIRM_URLCONF)


# ------------------------------------------------------------------------- REFUSAL: 1/5
# expired


def test_the_domain_refuses_an_expired_token(firm: Firm) -> None:
    # Given a portal invitation whose week has run out
    invite, raw_token = _issue(firm)
    invite.expires_at = timezone.now() - timedelta(seconds=1)
    invite.save(update_fields=["expires_at"])

    # Then `resolve_invite` — not the view — is what refuses it
    with pytest.raises(InviteExpiredError):
        resolve_invite(raw_token)


def test_an_expired_token_is_refused_at_the_portal_route(firm: Firm) -> None:
    # Given the same lapsed invitation
    invite, raw_token = _issue(firm)
    invite.expires_at = timezone.now() - timedelta(seconds=1)
    invite.save(update_fields=["expires_at"])

    # When it is presented on the portal host
    response = _register(raw_token)

    # Then it is GONE and nothing was created
    assert response.status_code == HTTPStatus.GONE
    assert not User.objects.filter(email=INVITED).exists()
    assert not Membership.objects.filter(client__isnull=False).exists()


# ------------------------------------------------------------------------- REFUSAL: 2/5
# revoked


def test_the_domain_refuses_a_withdrawn_token(firm: Firm) -> None:
    # Given a portal invitation the firm stood down
    invite, raw_token = _issue(firm)
    revoke_invite(actor=firm.owner, invite=invite)

    # Then `resolve_invite` refuses it, and as WITHDRAWN rather than as merely lapsed
    with pytest.raises(InviteRevokedError):
        resolve_invite(raw_token)


def test_a_revoked_token_is_refused_at_the_portal_route(firm: Firm) -> None:
    # Given the same withdrawn invitation
    invite, raw_token = _issue(firm)
    revoke_invite(actor=firm.owner, invite=invite)

    # When it is presented on the portal host
    response = _register(raw_token)

    # Then it is GONE, the row still exists carrying its withdrawal, and no seat
    # appeared
    assert response.status_code == HTTPStatus.GONE
    assert Invite.objects.get(pk=invite.pk).revoked_at is not None
    assert Invite.objects.get(pk=invite.pk).accepted_at is None
    assert not Membership.objects.filter(client__isnull=False).exists()


# ------------------------------------------------------------------------- REFUSAL: 3/5
# wrong tenant


def test_the_domain_refuses_an_invitation_from_another_firm(firm: Firm) -> None:
    # Given Acme's invitation, and Beta's portal door
    invite, _raw_token = _issue(firm)

    # Then the domain refuses it. The check lives here rather than only in the view so
    # that a management command or a future API cannot reach acceptance without it.
    with pytest.raises(InviteHostMismatchError):
        assert_portal_invite(invite=invite, firm_slug="beta")

    # …and the same call for the right firm does NOT raise, so the case above is not
    # passing because the helper refuses everything
    assert_portal_invite(invite=invite, firm_slug=firm.tenant.slug)


def test_another_firm_s_portal_host_refuses_the_link(firm: Firm) -> None:
    # Given Acme's portal invitation, and a second firm with its own portal host
    _beta = _firm(slug="beta", cnpjs=("11444777000161", "11222333000181"))
    _invite, raw_token = _issue(firm)

    # When the link is opened at Beta's door
    response = _register(raw_token, host=BETA_PORTAL)

    # Then it is NOT FOUND — never 403, which would confirm the token names a real
    # invitation somewhere on the platform — and no account or seat was created
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert not User.objects.filter(email=INVITED).exists()
    assert not Membership.objects.filter(client__isnull=False).exists()


def test_a_firm_side_invitation_is_refused_at_the_portal_route(firm: Firm) -> None:
    # Given a FIRM-side invitation, which carries no client
    invite, raw_token = issue_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        email=INVITED,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    assert invite.client_id is None, (
        "the fixture is not firm-side, so this proves nothing"
    )

    # Then the domain refuses it here, and the route answers 404
    with pytest.raises(InviteScopeMismatchError):
        assert_portal_invite(invite=invite, firm_slug=firm.tenant.slug)
    assert _register(raw_token).status_code == HTTPStatus.NOT_FOUND
    # Redeeming it would have minted the firm-wide row TenantMiddleware accepts as proof
    # of firm-side access, from a flow whose whole premise is that it grants one client.
    assert not Membership.objects.filter(client__isnull=True, role=invite.role).exists()


# ------------------------------------------------------------------------- REFUSAL: 4/5
# wrong client


def test_issuing_for_another_firm_s_client_is_not_found(firm: Firm) -> None:
    # Given a client that belongs to a DIFFERENT firm
    beta = _firm(slug="beta", cnpjs=("11444777000161", "11222333000181"))

    # When Acme's owner posts that client's id at their own subdomain
    response = _post_issue(firm, beta.padaria.pk)

    # Then it is 404, never 403: a 403 would confirm the id names a real client and turn
    # the address bar into an enumeration oracle for another firm's book
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert not Invite.objects.filter(email=INVITED).exists()
    assert mail.outbox == []


def test_a_portal_session_for_one_client_cannot_redeem_another_client_s_link(
    firm: Firm,
) -> None:
    # Given somebody holding a live portal seat in the padaria
    _first, first_token = _issue(firm, client=firm.padaria)
    _register(first_token)
    seated = User.objects.get(email=INVITED)
    enrol_totp(seated)
    padaria_seat = Membership.objects.get(user=seated, client=firm.padaria)

    # And an invitation into the OFICINA, addressed to somebody else entirely
    other, other_token = issue_client_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        client=firm.oficina,
        email="outro@qualquer.example",
        role=TenantRole.CLIENT_COLLABORATOR,
    )

    # When the padaria's user opens that link
    session = Client()
    session.force_login(seated)
    response = session.post(
        _accept_path(other_token),
        {},
        headers={"host": ACME_PORTAL},
    )

    # Then they are refused, no seat in the oficina appeared, their own seat is
    # untouched
    # and the invitation is still unspent for the person it was actually addressed to
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert not Membership.objects.filter(user=seated, client=firm.oficina).exists()
    padaria_seat.refresh_from_db()
    assert padaria_seat.client_id == firm.padaria.pk
    assert padaria_seat.is_active
    assert Invite.objects.get(pk=other.pk).accepted_at is None


def test_the_domain_refuses_the_wrong_client_s_session_directly(firm: Firm) -> None:
    # Given the same pairing, with HTTP taken out of the picture
    _first, first_token = _issue(firm, client=firm.padaria)
    _register(first_token)
    seated = User.objects.get(email=INVITED)
    _other, other_token = issue_client_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        client=firm.oficina,
        email="outro@qualquer.example",
        role=TenantRole.CLIENT_COLLABORATOR,
    )

    # Then the binding is enforced in the domain, not only in the view
    with pytest.raises(InviteEmailMismatchError):
        accept_invite(invite=resolve_invite(other_token), user=seated)
    assert not Membership.objects.filter(user=seated, client=firm.oficina).exists()


# ------------------------------------------------------------------------- REFUSAL: 5/5
# rate limited


def test_the_accept_post_is_registered_as_a_public_post_endpoint(firm: Firm) -> None:
    """Prove the WIRING, at the predicate the middleware actually consults.

    Asserting the url name is in the setting would prove only that a string was added to
    a list. `is_public_post_endpoint` is what decides, it resolves the path against the
    request's own urlconf, and the portal tree is a different tree — so this is where a
    route mounted correctly but named differently, or named correctly but mounted on the
    wrong tree, actually shows up.
    """
    _invite, raw_token = _issue(firm)
    # Cast rather than annotated: `urlconf` is read by Django's own handler but is not
    # declared on HttpRequest, which is exactly why apps/portal/middleware.py names it.
    request = cast(
        "DispatchableHttpRequest",
        Client()
        .request(REQUEST_METHOD="POST", PATH_INFO=_accept_path(raw_token))
        .wsgi_request,
    )
    request.urlconf = PORTAL_URLCONF

    assert is_public_post_endpoint(request)
    # …and a GET of the same path is not counted, so the limit cannot be exhausted by
    # anybody merely opening their own link twice.
    request.method = "GET"
    assert not is_public_post_endpoint(request)


def test_the_sixth_acceptance_attempt_from_one_address_is_refused(
    firm: Firm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a pinned counting window, so all six attempts land in one bucket
    pin_rate_limit_window(monkeypatch)
    _invite, raw_token = _issue(firm)
    path = _accept_path(raw_token)

    def attempt() -> int:
        return (
            Client()
            .post(
                path,
                {"full_name": "X", "password1": "nope", "password2": "nope"},
                headers={"host": ACME_PORTAL},
                REMOTE_ADDR="198.51.100.9",
            )
            .status_code
        )

    # When five attempts are made against a live token
    allowed = [attempt() for _ in range(LOGIN_ATTEMPTS_ALLOWED)]

    # Then every one of them is SERVED — this is the falsification the case needs, since
    # a middleware refusing everything would satisfy the assertion below on its own
    assert HTTPStatus.TOO_MANY_REQUESTS not in allowed, allowed

    # …and the sixth is refused by the rate limiter, before the view runs at all
    assert attempt() == HTTPStatus.TOO_MANY_REQUESTS
    assert not User.objects.filter(email=INVITED).exists()

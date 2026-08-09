"""An invite link is a bearer credential that travels through other people's systems.

It is forwarded, quoted in ticket threads, and archived in mailboxes nobody audits.
So possession of the link must NOT be sufficient to join an accounting firm: the
accepting session has to prove control of the invited mailbox as well. The cases
below pin that binding from both directions — the wrong person cannot accept, and the
right person still can.
"""

import logging
from datetime import timedelta
from http import HTTPStatus

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.http.response import HttpResponseBase
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import (
    InviteEmailMismatchError,
    InviteScope,
    accept_invite,
    resolve_invite,
)
from apps.accounts.models import User
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105
INVITED = "novo@alpha.example"
PLATFORM_HOST = "testserver"
TENANT_HOST = "alpha.localhost"


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.accounts.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha Contabilidade", slug="alpha")


def _firm_user(email: str, tenant: Tenant, role: str) -> User:
    user = User.objects.create_user(email=email, password=PASSWORD)
    Membership.objects.create(user=user, tenant=tenant, role=role)
    enrol_totp(user)
    return user


def _verified(user: User, email: str | None = None) -> EmailAddress:
    return EmailAddress.objects.create(
        user=user,
        email=email or user.email,
        verified=True,
        primary=True,
    )


def _issue(tenant: Tenant, email: str = INVITED) -> tuple[Invite, str]:
    return Invite.issue(tenant=tenant, email=email, role=TenantRole.STAFF_ACCOUNTANT)


def _accept_url(raw_token: str) -> str:
    return f"/convites/aceitar/{raw_token}/"


def _post_issue(client: Client, email: str = INVITED) -> HttpResponseBase:
    return client.post(
        "/convites/",
        {"email": email, "role": TenantRole.STAFF_ACCOUNTANT},
        headers={"host": TENANT_HOST},
    )


# --------------------------------------------------------------------------- issuance


def test_an_owner_may_issue_an_invite(tenant: Tenant) -> None:
    # Given a firm owner signed in on their firm's subdomain
    owner = _firm_user("owner@alpha.example", tenant, TenantRole.OWNER)
    client = Client()
    client.force_login(owner)

    # When they invite a new colleague
    response = _post_issue(client)

    # Then exactly one invite exists and exactly one message was sent
    assert response.status_code == HTTPStatus.FOUND
    assert Invite.objects.filter(tenant=tenant, email=INVITED).count() == 1
    assert len(mail.outbox) == 1
    assert INVITED in mail.outbox[0].to


def test_an_operations_admin_may_issue_an_invite(tenant: Tenant) -> None:
    # Given the other role the RBAC matrix permits to create users
    admin = _firm_user("ops@alpha.example", tenant, TenantRole.OPERATIONS_ADMIN)
    client = Client()
    client.force_login(admin)

    # When they invite a colleague
    response = _post_issue(client)

    # Then the invite is created
    assert response.status_code == HTTPStatus.FOUND
    assert Invite.objects.filter(tenant=tenant).count() == 1


def test_a_staff_accountant_may_not_issue_an_invite(tenant: Tenant) -> None:
    # Given a staff accountant, who is firm-side but not a user administrator
    accountant = _firm_user("cont@alpha.example", tenant, TenantRole.STAFF_ACCOUNTANT)
    client = Client()
    client.force_login(accountant)

    # When they try to invite someone
    response = _post_issue(client)

    # Then they are refused and nothing is created
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert Invite.objects.count() == 0
    assert mail.outbox == []


def test_the_raw_token_is_never_persisted(tenant: Tenant) -> None:
    # Given an issued invite
    invite, raw_token = _issue(tenant)

    # When every stored column value is scanned
    stored = [str(value) for row in Invite.objects.values() for value in row.values()]

    # Then the raw token appears nowhere — only its digest is held
    assert raw_token not in stored
    assert invite.token == Invite.hash_token(raw_token)
    assert invite.token != raw_token


def test_the_raw_token_is_never_logged(
    tenant: Tenant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given an owner issuing an invite and the invitee accepting it
    owner = _firm_user("owner@alpha.example", tenant, TenantRole.OWNER)
    client = Client()
    client.force_login(owner)

    with caplog.at_level(logging.DEBUG):
        _post_issue(client)
        raw_token = _token_from_outbox()
        Client().post(
            _accept_url(raw_token),
            {"full_name": "Novo", "password1": PASSWORD, "password2": PASSWORD},
        )

    # Then no emitted record carries the bearer value
    assert caplog.records, "nothing was logged — this check would pass vacuously"
    assert all(raw_token not in record.getMessage() for record in caplog.records)


def _token_from_outbox() -> str:
    body = mail.outbox[-1].body
    marker = "/convites/aceitar/"
    start = body.index(marker) + len(marker)
    return body[start:].split("/", 1)[0]


# ------------------------------------------------------------------------- acceptance


def test_a_new_invitee_registers_and_gets_exactly_one_membership(
    tenant: Tenant,
) -> None:
    # Given an invite for someone with no account yet
    _invite, raw_token = _issue(tenant)

    # When they set a password on the acceptance page
    response = Client().post(
        _accept_url(raw_token),
        {"full_name": "Nova Pessoa", "password1": PASSWORD, "password2": PASSWORD},
    )

    # Then exactly one user and exactly one membership exist, and the invite is spent
    assert response.status_code == HTTPStatus.FOUND
    user = User.objects.get(email=INVITED)
    memberships = Membership.objects.filter(user=user, tenant=tenant)
    assert memberships.count() == 1
    assert memberships.get().role == TenantRole.STAFF_ACCOUNTANT
    assert Invite.objects.get().accepted_at is not None
    # The token was delivered to that mailbox, so accepting it proves control of it.
    assert EmailAddress.objects.filter(user=user, email=INVITED, verified=True).exists()


def test_replaying_an_accepted_token_is_refused(tenant: Tenant) -> None:
    # Given an invite that has already been accepted
    _invite, raw_token = _issue(tenant)
    first = Client().post(
        _accept_url(raw_token),
        {"full_name": "Nova Pessoa", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert first.status_code == HTTPStatus.FOUND

    # When the same link is used a second time
    second = Client().post(
        _accept_url(raw_token),
        {"full_name": "Outra", "password1": PASSWORD, "password2": PASSWORD},
    )

    # Then it is refused, and no second membership or user appeared
    assert second.status_code == HTTPStatus.GONE
    assert Membership.objects.count() == 1
    assert User.objects.filter(email=INVITED).count() == 1


def test_an_expired_token_is_refused(tenant: Tenant) -> None:
    # Given an invite whose validity has lapsed
    invite, raw_token = _issue(tenant)
    invite.expires_at = timezone.now() - timedelta(seconds=1)
    invite.save(update_fields=["expires_at"])

    # When it is presented
    response = Client().post(
        _accept_url(raw_token),
        {"full_name": "Nova Pessoa", "password1": PASSWORD, "password2": PASSWORD},
    )

    # Then it is refused and nothing is created
    assert response.status_code == HTTPStatus.GONE
    assert Membership.objects.count() == 0
    assert User.objects.filter(email=INVITED).count() == 0


def test_an_unknown_token_is_refused(tenant: Tenant) -> None:
    # Given a token that was never issued
    _issue(tenant)

    # When it is presented
    response = Client().get(_accept_url("nao-existe-este-token"))

    # Then it is refused
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert Membership.objects.count() == 0


def test_accepting_while_signed_in_as_a_different_user_is_refused(
    tenant: Tenant,
) -> None:
    # Given an invite for one address, and a session belonging to a DIFFERENT person
    # who happens to hold the link — forwarded, or lifted from a ticket thread
    _invite, raw_token = _issue(tenant)
    intruder = User.objects.create_user(
        email="outro@qualquer.example",
        password=PASSWORD,
    )
    _verified(intruder)
    client = Client()
    client.force_login(intruder)

    # When they open the acceptance link
    response = client.post(
        _accept_url(raw_token),
        {"full_name": "Intruso", "password1": PASSWORD, "password2": PASSWORD},
    )

    # Then they are refused, and — the point of this case — no membership was silently
    # attached to request.user, and the invite is still unspent for its real recipient
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert Membership.objects.filter(user=intruder).count() == 0
    assert Membership.objects.count() == 0
    assert Invite.objects.get().accepted_at is None


def test_an_existing_user_with_the_invited_verified_address_may_accept(
    tenant: Tenant,
) -> None:
    # Given the invited person, who already has an account elsewhere on the platform
    other = Tenant.objects.create(name="Beta", slug="beta")
    invitee = _firm_user(INVITED, other, TenantRole.STAFF_ACCOUNTANT)
    _verified(invitee)
    _invite, raw_token = _issue(tenant)
    client = Client()
    client.force_login(invitee)

    # When they accept
    response = client.post(_accept_url(raw_token), {})

    # Then a membership is added to the new firm without duplicating the account
    assert response.status_code == HTTPStatus.FOUND
    assert Membership.objects.filter(user=invitee, tenant=tenant).count() == 1
    assert User.objects.filter(email=INVITED).count() == 1


def test_an_existing_user_whose_address_is_unverified_may_not_accept(
    tenant: Tenant,
) -> None:
    # Given a session whose address matches the invite but was never verified
    invitee = User.objects.create_user(email=INVITED, password=PASSWORD)
    EmailAddress.objects.create(
        user=invitee,
        email=INVITED,
        verified=False,
        primary=True,
    )
    _invite, raw_token = _issue(tenant)
    client = Client()
    client.force_login(invitee)

    # When they accept
    response = client.post(_accept_url(raw_token), {})

    # Then it is refused. An unverified address is a claim, not proof of control.
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert Membership.objects.filter(user=invitee).count() == 0


def test_the_domain_layer_refuses_a_mismatched_user_directly(tenant: Tenant) -> None:
    # Given an invite and an unrelated account, bypassing the HTTP layer entirely
    _invite_row, raw_token = _issue(tenant)
    intruder = User.objects.create_user(email="outro@qualquer.example")
    _verified(intruder)
    invite = resolve_invite(raw_token)

    # When acceptance is attempted for that account
    # Then the binding is enforced in the domain, not only in the view — so a future
    # caller that forgets the view's check cannot bypass it
    with pytest.raises(InviteEmailMismatchError):
        accept_invite(
            invite=invite,
            user=intruder,
            expected_scope=InviteScope.FIRM,
        )
    assert Membership.objects.count() == 0
    assert Invite.objects.get().accepted_at is None

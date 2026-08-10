"""Pin that opening a bearer-credential link never spends the credential."""

import os
import re
import subprocess
import sys
from http import HTTPStatus

import pytest
from allauth.account import app_settings
from allauth.account.models import EmailAddress, get_emailconfirmation_model
from django.core import mail
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership, Tenant, TenantRole


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


def _email_address(user: User, *, verified: bool) -> EmailAddress:
    return EmailAddress.objects.create(
        user=user,
        email=user.email,
        verified=verified,
        primary=True,
    )


def test_confirm_email_on_get_is_disabled_in_test_and_production() -> None:
    assert app_settings.CONFIRM_EMAIL_ON_GET is False

    command = (
        "from allauth.account import app_settings; "
        "print(app_settings.CONFIRM_EMAIL_ON_GET)"
    )
    environment = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.prod"}
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", command],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.stdout.strip() == "False"


@pytest.mark.django_db(transaction=True)
def test_get_does_not_consume_firm_or_portal_invitations() -> None:
    tenant = Tenant.objects.create(name="Alpha Contabilidade", slug="alpha")
    with tenant_context(tenant.pk):
        # ALL_OBJECTS_OK: fixture seeding under an established tenant context.
        company = ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="PADARIA ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )

    firm_user = User.objects.create_user(email="firm@alpha.example")
    portal_user = User.objects.create_user(email="portal@alpha.example")
    _email_address(firm_user, verified=True)
    _email_address(portal_user, verified=True)
    firm_invite, firm_token = Invite.issue(
        tenant=tenant,
        email=firm_user.email,
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    portal_invite, portal_token = Invite.issue(
        tenant=tenant,
        client=company,
        email=portal_user.email,
        role=TenantRole.CLIENT_OWNER,
    )

    firm_browser = Client()
    firm_browser.force_login(firm_user)
    firm_response = firm_browser.get(reverse("invite-accept", args=[firm_token]))

    portal_browser = Client()
    portal_browser.force_login(portal_user)
    portal_response = portal_browser.get(
        reverse(
            "portal-invite-accept",
            args=[portal_token],
            urlconf="apps.portal.urls",
        ),
        headers={"host": "alpha-portal.localhost"},
    )

    assert firm_response.status_code == HTTPStatus.OK
    assert portal_response.status_code == HTTPStatus.OK
    for invite in (firm_invite, portal_invite):
        invite.refresh_from_db()
        assert invite.accepted_at is None
        assert invite.revoked_at is None
    assert not Membership.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_confirm_email_get_leaves_the_address_unverified_until_post() -> None:
    user = User.objects.create_user(email="confirm@alpha.example")
    address = _email_address(user, verified=False)
    confirmation = get_emailconfirmation_model().create(address)
    confirmation_url = reverse("account_confirm_email", args=[confirmation.key])
    browser = Client()

    get_response = browser.get(confirmation_url)

    assert get_response.status_code == HTTPStatus.OK
    address.refresh_from_db()
    assert not address.verified

    post_response = browser.post(confirmation_url)

    assert post_response.status_code in range(300, 400)
    address.refresh_from_db()
    assert address.verified


@pytest.mark.django_db(transaction=True)
def test_reset_key_get_preserves_the_key_for_the_following_post() -> None:
    user = User.objects.create_user(email="reset@alpha.example")
    _email_address(user, verified=True)
    browser = Client()
    requested = browser.post(
        reverse("account_reset_password"),
        {"email": user.email},
    )
    assert requested.status_code in range(300, 400)
    assert mail.outbox
    reset_path = re.search(
        r"/accounts/password/reset/key/[\w-]+/",
        str(mail.outbox[-1].body),
    )
    assert reset_path is not None

    get_response = browser.get(reset_path.group(0))

    assert get_response.status_code in range(300, 400)
    form_url = get_response["Location"]
    new_password = f"Reset-{user.pk}-safe"
    post_response = browser.post(
        form_url,
        {"password1": new_password, "password2": new_password},
    )

    assert post_response.status_code in range(300, 400)
    user.refresh_from_db()
    assert user.check_password(new_password)

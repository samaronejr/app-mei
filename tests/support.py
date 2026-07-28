"""Helpers shared across suites, kept out of any one feature's test module."""

from http import HTTPStatus

from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User

TOTP_SECRET = "MFRGGZDFMZTWQ2LKNNWG23TPOBYGC4TT"  # noqa: S105


def enrol_totp(user: User) -> None:
    """Give a firm-side account the second factor the product requires of it.

    Anyone holding an active `Membership` — or `is_staff` — is diverted to enrolment
    before reaching any view, so a fixture that builds a firm-side user and skips this
    is exercising that redirect rather than whatever it meant to assert.
    """
    TOTP.activate(user, TOTP_SECRET)


def totp_code(secret: str = TOTP_SECRET) -> str:
    """Compute a code the real verifier will accept, right now.

    Derived from allauth's own primitives rather than switching on its insecure
    bypass code, so the login path under test is the one that ships.
    """
    counter = next(iter(yield_hotp_counters_from_time()))
    return str(format_hotp_value(hotp_value(secret, counter)))


def sign_in(user: User, password: str, *, with_mfa: bool = False) -> Client:
    """Drive the real login flow, including the second factor when one is enrolled."""
    EmailAddress.objects.get_or_create(
        user=user,
        email=user.email,
        defaults={"verified": True, "primary": True},
    )
    client = Client()
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": password},
    )
    assert response.status_code == HTTPStatus.FOUND, "password was not accepted"
    if with_mfa:
        response = client.post(reverse("mfa_authenticate"), {"code": totp_code()})
        assert response.status_code == HTTPStatus.FOUND, "TOTP code was not accepted"
    return client

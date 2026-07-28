"""Login must not answer the question "does this address have an account here?".

A login form is an oracle by default: a wrong password and an unknown address take
different code paths, and any observable difference between the two responses — a
distinct message, a different template, a different status — enumerates the customer
list of every firm on the platform. The assertion below compares the responses byte
for byte with only the per-request CSRF token normalized away.
"""

import importlib
import re
from http import HTTPStatus

import pytest
from allauth.account import app_settings as account_settings
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105
WRONG_PASSWORD = "not-the-password"  # noqa: S105

_CSRF_VALUE = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]+"')


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.accounts.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


def _normalize(body: bytes, submitted_email: str) -> bytes:
    """Strip the two values that legitimately differ between two POST responses.

    The per-request CSRF token, and the address the form redisplays — which is the
    caller's own input and therefore discloses nothing they did not already know.
    Substituting the submitted value rather than blanking the whole field is
    deliberate: any *other* difference in the body still trips the assertion.
    """
    without_csrf = _CSRF_VALUE.sub(b'name="csrfmiddlewaretoken" value="X"', body)
    return without_csrf.replace(submitted_email.encode(), b"SUBMITTED")


def _attempt(email: str, password: str) -> tuple[int, bytes]:
    client = Client()
    response = client.post(
        reverse("account_login"),
        {"login": email, "password": password},
    )
    return response.status_code, _normalize(response.content, email)


def test_a_wrong_password_is_indistinguishable_from_an_unknown_address() -> None:
    # Given one address that exists and one that does not
    User.objects.create_user(email="known@alpha.example", password=PASSWORD)

    # When both are submitted with a password that will not authenticate
    known_status, known_body = _attempt("known@alpha.example", WRONG_PASSWORD)
    unknown_status, unknown_body = _attempt("unknown@alpha.example", WRONG_PASSWORD)

    # Then neither the status nor the body reveals which address exists
    assert known_status == unknown_status == HTTPStatus.OK
    assert known_body == unknown_body


def test_a_correct_password_still_authenticates() -> None:
    # Given a registered address
    User.objects.create_user(email="known@alpha.example", password=PASSWORD)

    # When the correct password is submitted
    status, body = _attempt("known@alpha.example", PASSWORD)

    # Then the response differs from the failure case. Without this the equality above
    # would be satisfied by a login view that rejects absolutely everything.
    failed_status, failed_body = _attempt("known@alpha.example", WRONG_PASSWORD)
    assert (status, body) != (failed_status, failed_body)


def test_login_is_by_email_and_there_is_no_username_field() -> None:
    # Given the shipped account configuration
    # When the login methods are read
    # Then email is the only one, matching accounts.User having no username column
    assert set(account_settings.LOGIN_METHODS) == {"email"}
    assert "username" not in account_settings.SIGNUP_FIELDS
    assert not hasattr(User, "username")


def test_email_verification_is_mandatory() -> None:
    # Given the shipped account configuration
    # When the verification policy is read
    # Then an unverified address cannot be used to sign in
    assert account_settings.EMAIL_VERIFICATION == "mandatory"


def test_enumeration_prevention_is_on() -> None:
    # Given the shipped account configuration
    # When the enumeration policy is read
    # Then allauth's own protection is enabled rather than left to the default
    assert account_settings.PREVENT_ENUMERATION is True


def test_the_session_cookie_is_hardened_under_production_settings() -> None:
    # Given the production settings module
    prod = importlib.import_module("config.settings.prod")

    # When its cookie policy is read
    # Then the session cookie is Secure, HttpOnly, SameSite=Lax and host-only
    assert prod.SESSION_COOKIE_SECURE is True
    assert prod.SESSION_COOKIE_HTTPONLY is True
    assert prod.SESSION_COOKIE_SAMESITE == "Lax"
    assert prod.SESSION_COOKIE_DOMAIN is None
    assert prod.SESSION_COOKIE_NAME == "__Host-sessionid"

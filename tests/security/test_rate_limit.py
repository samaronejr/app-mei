"""The credential limit must be tenant-independent, and this suite is the proof.

An earlier draft of the plan keyed the login limit on the tenant and asserted the
bypass as if it were a feature: "tenant B's login still succeeds after tenant A's
limit is exhausted". Credentials here are platform-global — one `accounts.User` table,
email as the username — so that key lets an attacker rotate the `Host` header for five
attempts per minute *per firm on the platform*, and read the correct-password signal
from any of them.

`test_the_login_limit_follows_the_email_across_subdomains` is that scenario inverted
into an assertion.
"""

from http import HTTPStatus

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.tenants.models import Tenant
from tests.support import pin_rate_limit_window

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105
WRONG = "not-the-password"
EMAIL = "alvo@alpha.example"
LOGIN_ATTEMPTS_ALLOWED = 5
IP_ATTEMPTS_ALLOWED = 20


@pytest.fixture(autouse=True)
def _pinned_rate_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Count every attempt in one window, which is what "within a minute" means.

    django-ratelimit's window is fixed, not sliding: `_get_window` computes
    `ts - (ts % 60) + (crc32(bucket_key) % 60)` and that value is hashed into the cache
    key, so each bucket resets at one fixed second of every wall-clock minute -- second
    16 for `email:alvo@alpha.example`. An attempt landing after that instant is counted
    against a *fresh* key starting at one, so the request this suite exists to see
    refused is served instead.

    Measured, not assumed: twelve thousand real-clock runs of the first test below
    produced exactly six failures over 6.57 minutes -- one per boundary crossing, every
    one at second 16, none without a roll. Exposure is `elapsed / 60`, so it grows with
    any slower or busier runner. It is wall-clock phase leaking into the assertion, not
    state surviving between tests: this suite's cache is LocMem and `tests/conftest.py`
    already empties it around every test.

    Pinning weakens nothing. The limiter still runs, the budget is still five, and the
    sixth attempt must still be refused; only the guarantee that all six are counted
    against one bucket is added -- the precondition each test name already claims.
    """
    pin_rate_limit_window(monkeypatch)


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.accounts.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture(autouse=True)
def _two_firms() -> tuple[Tenant, Tenant]:
    return (
        Tenant.objects.create(name="Firma A", slug="firma-a"),
        Tenant.objects.create(name="Firma B", slug="firma-b"),
    )


def _attempt(email: str, host: str = "testserver", ip: str = "203.0.113.7") -> int:
    return (
        Client()
        .post(
            reverse("account_login"),
            {"login": email, "password": WRONG},
            headers={"host": host},
            REMOTE_ADDR=ip,
        )
        .status_code
    )


def test_the_sixth_attempt_for_one_email_within_a_minute_is_refused() -> None:
    # Given an address being guessed at
    User.objects.create_user(email=EMAIL, password=PASSWORD)

    # When five attempts are made
    allowed = [_attempt(EMAIL) for _ in range(LOGIN_ATTEMPTS_ALLOWED)]

    # Then all five are served, and the sixth is not
    assert allowed == [HTTPStatus.OK] * LOGIN_ATTEMPTS_ALLOWED
    assert _attempt(EMAIL) == HTTPStatus.TOO_MANY_REQUESTS


def test_the_login_limit_follows_the_email_across_subdomains() -> None:
    # Given an address whose budget has been spent entirely on one firm's subdomain
    User.objects.create_user(email=EMAIL, password=PASSWORD)
    for _ in range(LOGIN_ATTEMPTS_ALLOWED):
        _attempt(EMAIL, host="firma-a.localhost")
    assert _attempt(EMAIL, host="firma-a.localhost") == HTTPStatus.TOO_MANY_REQUESTS

    # When the attacker rotates the Host header to a different firm
    on_other_firm = _attempt(EMAIL, host="firma-b.localhost")

    # Then the bucket follows the credential, not the hostname. A tenant-keyed limit
    # would have handed out a fresh five attempts here — and another five for every
    # further firm on the platform.
    assert on_other_firm == HTTPStatus.TOO_MANY_REQUESTS


def test_the_platform_host_shares_the_same_bucket_as_the_subdomains() -> None:
    # Given a budget spent on a firm's subdomain
    User.objects.create_user(email=EMAIL, password=PASSWORD)
    for _ in range(LOGIN_ATTEMPTS_ALLOWED):
        _attempt(EMAIL, host="firma-a.localhost")

    # When the same address is tried on the platform host
    # Then it is refused there too
    assert _attempt(EMAIL, host="testserver") == HTTPStatus.TOO_MANY_REQUESTS


def test_two_different_addresses_do_not_share_a_bucket() -> None:
    # Given one address that has exhausted its budget
    for _ in range(LOGIN_ATTEMPTS_ALLOWED + 1):
        _attempt(EMAIL)
    assert _attempt(EMAIL) == HTTPStatus.TOO_MANY_REQUESTS

    # When a different address is tried from the same IP
    other = _attempt("outro@alpha.example")

    # Then it is served. Without this control the limit could be satisfied by a
    # middleware that refuses everything after any six requests.
    assert other == HTTPStatus.OK


def test_the_ip_limit_applies_regardless_of_address() -> None:
    # Given twenty attempts from one IP, each for a different address so that no
    # single email bucket is ever exhausted
    for index in range(IP_ATTEMPTS_ALLOWED):
        assert _attempt(f"alvo{index}@alpha.example") == HTTPStatus.OK

    # When a twenty-first address is tried from that same IP
    # Then it is refused: spreading attempts across addresses does not evade the limit
    assert _attempt("alvo-extra@alpha.example") == HTTPStatus.TOO_MANY_REQUESTS


def test_a_different_ip_has_its_own_budget() -> None:
    # Given an IP that has exhausted its budget
    for index in range(IP_ATTEMPTS_ALLOWED + 1):
        _attempt(f"alvo{index}@alpha.example")

    # When the same kind of attempt arrives from elsewhere
    # Then it is served
    assert _attempt("outro@alpha.example", ip="198.51.100.4") == HTTPStatus.OK


def test_the_limits_are_read_from_settings_not_hardcoded() -> None:
    # Given the shipped configuration
    # When the four rates are read
    # Then they carry the values the plan specifies, from settings
    assert settings.RATELIMIT_LOGIN_EMAIL == "5/m"
    assert settings.RATELIMIT_LOGIN_IP == "20/m"
    assert settings.RATELIMIT_WRITE == "60/m"
    assert settings.RATELIMIT_READ == "120/m"


def test_the_refusal_is_429_with_a_portuguese_message() -> None:
    # Given an exhausted bucket
    for _ in range(LOGIN_ATTEMPTS_ALLOWED + 1):
        _attempt(EMAIL)

    # When one more attempt is made
    response = Client().post(
        reverse("account_login"),
        {"login": EMAIL, "password": WRONG},
    )

    # Then the caller is told so in the product's language
    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert "Muitas requisições" in response.content.decode()

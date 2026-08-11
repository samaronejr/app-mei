"""Helpers shared across suites, kept out of any one feature's test module."""

from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from typing import Final

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa import app_settings
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.test import Client
from django.urls import reverse
from django_ratelimit import core as ratelimit_core

from apps.accounts.models import User

TOTP_SECRET = "MFRGGZDFMZTWQ2LKNNWG23TPOBYGC4TT"  # noqa: S105

# Any fixed instant. Only its constancy matters, and `conftest` empties the buckets
# around every test, so reusing one across the suite cannot carry a count forward.
PINNED_INSTANT: Final = 1_800_000_000.0


class _PinnedClock:
    def __init__(self, instant: float = PINNED_INSTANT) -> None:
        self._instant = instant

    def time(self) -> float:
        """Return the pinned instant, so every request lands in one window."""
        return self._instant

    def advance(self, seconds: float) -> None:
        self._instant += seconds


def pin_rate_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a rate-limit budget from being split across two counting windows.

    django-ratelimit derives its cache key from `int(time.time())` bucketed into
    fixed periods, so the counter resets the instant a window rolls. A test that
    issues a 120-request budget and asserts the next one is refused therefore depends
    on the whole budget being issued inside a single wall-clock minute: it passes on a
    fast machine and fails on a slow runner, where the request that should be refused
    is the first of a fresh window and is served.

    That is wall-clock dependence in the test, not a defect in the limiter — proven by
    holding the clock still and rolling it by exactly one period, which flips the same
    request between 429 and 200. Pinning the clock removes the dependence without
    touching a single assertion: the budget, the refusal, and the per-user separation
    are all still exercised.

    Only `django_ratelimit.core`'s reference to `time` is replaced, so nothing else in
    the process — least of all the deadline arithmetic this product is built on — sees
    a frozen clock.
    """
    monkeypatch.setattr(ratelimit_core, "time", _PinnedClock())


@contextmanager
def pinned_totp_window(*, step: int = 0) -> Iterator[None]:
    instant = PINNED_INSTANT + (step * app_settings.TOTP_PERIOD)
    clock = _PinnedClock(instant)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(totp_auth, "time", clock)
        yield


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


def sign_in(
    user: User,
    password: str,
    *,
    with_mfa: bool = False,
    totp_step: int = 0,
) -> Client:
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
        with pinned_totp_window(step=totp_step):
            response = client.post(reverse("mfa_authenticate"), {"code": totp_code()})
        assert response.status_code == HTTPStatus.FOUND, "TOTP code was not accepted"
    return client

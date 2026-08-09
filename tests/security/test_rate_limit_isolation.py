import socket
from collections import Counter
from dataclasses import dataclass
from http import HTTPStatus
from itertools import cycle, islice
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings as django_settings
from django.core.cache import caches
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.urls import reverse
from django_ratelimit import core as ratelimit_core
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.security.ratelimit import credential_limits
from apps.tenants.models import Invite, Tenant, TenantRole
from tests.support import PINNED_INSTANT, pin_rate_limit_window

if TYPE_CHECKING:
    from apps.portal.middleware import DispatchableHttpRequest

pytestmark = pytest.mark.django_db(transaction=True)

FIRM_URLCONF = "config.urls"
PORTAL_URLCONF = "apps.portal.urls"
SOURCE_IP = "203.0.113.7"
TARGET_IP = "198.51.100.4"
PASSWORD = "correct-password"  # noqa: S105


@dataclass(frozen=True, slots=True)
class _Route:
    name: str
    urlconf: str
    host: str
    group: str
    key_prefix: str
    rate_setting: str
    address_field: str | None = None


_LOGIN = _Route(
    "account_login",
    FIRM_URLCONF,
    "testserver",
    "login-email",
    "login",
    "RATELIMIT_LOGIN_EMAIL",
    "login",
)
_RESET_REQUEST = _Route(
    "account_reset_password",
    FIRM_URLCONF,
    "testserver",
    "reset-request-email",
    "reset-request",
    "RATELIMIT_RESET_REQUEST_EMAIL",
    "email",
)
_DSR = _Route(
    "dsr-submit",
    FIRM_URLCONF,
    "testserver",
    "dsr-email",
    "dsr",
    "RATELIMIT_DSR_EMAIL",
    "email",
)
_INVITE = _Route(
    "invite-accept",
    FIRM_URLCONF,
    "testserver",
    "invite-accept-ip",
    "invite-accept",
    "RATELIMIT_INVITE_ACCEPT_IP",
)
_PORTAL_INVITE = _Route(
    "portal-invite-accept",
    PORTAL_URLCONF,
    "acme-portal.localhost",
    "portal-invite-accept-ip",
    "portal-invite-accept",
    "RATELIMIT_PORTAL_INVITE_ACCEPT_IP",
)
_MFA = _Route(
    "mfa_authenticate",
    FIRM_URLCONF,
    "testserver",
    "mfa-authenticate-ip",
    "mfa-authenticate",
    "RATELIMIT_MFA_AUTHENTICATE_IP",
)
_RESET_FROM_KEY = _Route(
    "account_reset_password_from_key",
    FIRM_URLCONF,
    "testserver",
    "reset-from-key-ip",
    "reset-from-key",
    "RATELIMIT_RESET_FROM_KEY_IP",
)

_ADDRESS_ROUTES: Final = (_LOGIN, _RESET_REQUEST, _DSR)
_ADDRESSLESS_ROUTES: Final = (
    _INVITE,
    _PORTAL_INVITE,
    _MFA,
    _RESET_FROM_KEY,
)
_PUBLIC_ROUTES: Final = _ADDRESS_ROUTES + _ADDRESSLESS_ROUTES


def _path(route: _Route, index: int, credential: str | None = None) -> str:
    supplied = credential or f"invalid-token-{index}"
    if route is _INVITE or route is _PORTAL_INVITE:
        return reverse(route.name, args=[supplied], urlconf=route.urlconf)
    if route is _RESET_FROM_KEY:
        return reverse(
            route.name,
            kwargs={"uidb36": "0", "key": supplied},
            urlconf=route.urlconf,
        )
    return reverse(route.name, urlconf=route.urlconf)


def _payload(route: _Route, index: int, email: str | None = None) -> dict[str, str]:
    submitted = email or f"person-{index}@example.invalid"
    if route is _LOGIN:
        return {"login": submitted, "password": "wrong-password"}
    if route is _RESET_REQUEST or route is _DSR:
        return {"email": submitted}
    if route is _MFA:
        return {"code": f"invalid-code-{index}"}
    if route is _RESET_FROM_KEY:
        return {"password1": "invalid", "password2": "invalid"}
    return {}


def _post(
    route: _Route,
    ip: str,
    *,
    index: int = 0,
    email: str | None = None,
    credential: str | None = None,
) -> HttpResponse:
    response = Client().post(
        _path(route, index, credential),
        _payload(route, index, email),
        headers={"host": route.host},
        REMOTE_ADDR=ip,
    )
    return cast("HttpResponse", response)


def _allowed(route: _Route) -> int:
    rate = cast("str", getattr(django_settings, route.rate_setting))
    return int(rate.split("/", maxsplit=1)[0])


def _assert_endpoint_isolation(source: _Route, target: _Route) -> None:
    source_codes = [
        _post(source, ip=SOURCE_IP, index=index).status_code
        for index in range(_allowed(source))
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in source_codes
    assert (
        _post(source, ip=SOURCE_IP, index=_allowed(source)).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )
    assert _post(target, ip=TARGET_IP).status_code != HTTPStatus.TOO_MANY_REQUESTS


def _exhaust_email_bucket(route: _Route, email: str) -> None:
    served = [
        _post(route, ip=SOURCE_IP, index=index, email=email).status_code
        for index in range(_allowed(route))
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(route, ip=SOURCE_IP, index=_allowed(route), email=email).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )


@pytest.fixture(autouse=True)
def _pinned_rate_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_rate_limit_window(monkeypatch)


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = FIRM_URLCONF
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


def test_flooding_mfa_does_not_limit_portal_invite_accept() -> None:
    _assert_endpoint_isolation(_MFA, _PORTAL_INVITE)


_OTHER_ADDRESSLESS_PAIRS: Final = tuple(
    (source, target)
    for source in _ADDRESSLESS_ROUTES
    for target in _ADDRESSLESS_ROUTES
    if source is not target and (source, target) != (_MFA, _PORTAL_INVITE)
)


@pytest.mark.parametrize(
    ("source", "target"),
    _OTHER_ADDRESSLESS_PAIRS,
    ids=[
        f"{source.name}-to-{target.name}" for source, target in _OTHER_ADDRESSLESS_PAIRS
    ],
)
def test_flooding_one_addressless_endpoint_does_not_limit_another(
    source: _Route,
    target: _Route,
) -> None:
    _assert_endpoint_isolation(source, target)


@pytest.mark.parametrize("route", _PUBLIC_ROUTES, ids=lambda route: route.name)
def test_each_public_post_has_the_approved_group_and_key_prefix(route: _Route) -> None:
    email = " Mixed@Example.COM " if route.address_field else None
    request = cast(
        "DispatchableHttpRequest",
        RequestFactory().post(
            _path(route, 0),
            _payload(route, 0, email),
            REMOTE_ADDR=SOURCE_IP,
        ),
    )
    request.urlconf = route.urlconf

    endpoint, umbrella = credential_limits(cast("HttpRequest", request))

    identity = "email:mixed@example.com" if email else f"ip:{SOURCE_IP}"
    assert endpoint.group == route.group
    assert endpoint.rate == getattr(django_settings, route.rate_setting)
    assert endpoint.key(endpoint.group, request) == f"{route.key_prefix}:{identity}"
    assert umbrella.group == "login-ip"
    assert umbrella.rate == django_settings.RATELIMIT_LOGIN_IP
    assert umbrella.key(umbrella.group, request) == f"ip:{SOURCE_IP}"


@pytest.mark.parametrize("landing_route", _PUBLIC_ROUTES, ids=lambda route: route.name)
def test_login_ip_umbrella_covers_every_registered_public_post(
    landing_route: _Route,
) -> None:
    umbrella_allowed = int(
        django_settings.RATELIMIT_LOGIN_IP.split("/", maxsplit=1)[0],
    )
    sequence = tuple(islice(cycle(_PUBLIC_ROUTES), umbrella_allowed))
    endpoint_counts = Counter(route.name for route in sequence)
    submitted_emails: list[str] = []

    assert {route.name for route in _PUBLIC_ROUTES} == set(
        django_settings.RATELIMIT_PUBLIC_POST_URL_NAMES,
    )
    assert set(endpoint_counts) == {route.name for route in _PUBLIC_ROUTES}
    assert all(
        endpoint_counts[route.name] < _allowed(route) for route in _PUBLIC_ROUTES
    )

    for index, route in enumerate(sequence):
        email = None
        if route.address_field is not None:
            email = f"{route.key_prefix}-{index}@example.invalid"
            submitted_emails.append(email)
        response = _post(route, ip=SOURCE_IP, index=index, email=email)
        assert response.status_code != HTTPStatus.TOO_MANY_REQUESTS, (
            f"request {index + 1} to {route.name} was masked by a tighter bucket"
        )

    assert len(submitted_emails) == len(set(submitted_emails))
    assert endpoint_counts[landing_route.name] + 1 < _allowed(landing_route)
    final_email = (
        "umbrella-final@example.invalid"
        if landing_route.address_field is not None
        else None
    )
    final = _post(
        landing_route,
        ip=SOURCE_IP,
        index=umbrella_allowed,
        email=final_email,
    )
    assert final.status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_repeated_requests_to_one_valid_invitation_token_are_limited() -> None:
    firm = Tenant.objects.create(name="Acme Contabilidade", slug="acme")
    _invite, raw_token = Invite.issue(
        tenant=firm,
        email="convidada@acme.example",
        role=TenantRole.STAFF_ACCOUNTANT,
    )

    served = [
        _post(_INVITE, ip=SOURCE_IP, credential=raw_token).status_code
        for _ in range(_allowed(_INVITE))
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(_INVITE, ip=SOURCE_IP, credential=raw_token).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )


def test_invalid_invitation_token_rotation_cannot_mint_new_buckets() -> None:
    served = [
        _post(_INVITE, ip=SOURCE_IP, index=index).status_code
        for index in range(_allowed(_INVITE))
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(_INVITE, ip=SOURCE_IP, index=_allowed(_INVITE)).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )


def test_one_ip_flood_is_refused_by_the_endpoint_counter() -> None:
    served = [
        _post(_RESET_FROM_KEY, ip=SOURCE_IP, index=index).status_code
        for index in range(_allowed(_RESET_FROM_KEY))
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(
            _RESET_FROM_KEY,
            ip=SOURCE_IP,
            index=_allowed(_RESET_FROM_KEY),
        ).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )


def test_the_same_endpoint_has_an_independent_budget_per_ip() -> None:
    served = [
        _post(_PORTAL_INVITE, ip=SOURCE_IP, index=index).status_code
        for index in range(_allowed(_PORTAL_INVITE))
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(
            _PORTAL_INVITE,
            ip=SOURCE_IP,
            index=_allowed(_PORTAL_INVITE),
        ).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )
    assert (
        _post(_PORTAL_INVITE, ip=TARGET_IP).status_code != HTTPStatus.TOO_MANY_REQUESTS
    )


def test_exhausting_dsr_for_a_victim_does_not_limit_their_login() -> None:
    victim_email = "victim@x.example"
    User.objects.create_user(email=victim_email, password=PASSWORD)

    _exhaust_email_bucket(_DSR, victim_email)

    assert _post(_LOGIN, ip=TARGET_IP, email=victim_email).status_code == HTTPStatus.OK


def test_exhausting_reset_requests_for_a_victim_does_not_limit_their_login() -> None:
    victim_email = "victim@x.example"
    victim = User.objects.create_user(
        email=victim_email,
        password=PASSWORD,
    )
    EmailAddress.objects.create(
        user=victim,
        email=victim_email,
        verified=True,
        primary=True,
    )

    _exhaust_email_bucket(_RESET_REQUEST, victim_email)

    assert _post(_LOGIN, ip=TARGET_IP, email=victim_email).status_code == HTTPStatus.OK


@pytest.mark.parametrize("route", [_DSR, _RESET_REQUEST], ids=lambda route: route.name)
def test_email_rotation_cannot_bypass_the_per_ip_umbrella(route: _Route) -> None:
    umbrella_allowed = int(
        django_settings.RATELIMIT_LOGIN_IP.split("/", maxsplit=1)[0],
    )
    served = [
        _post(
            route,
            ip=SOURCE_IP,
            index=index,
            email=f"rotated-{index}@example.invalid",
        ).status_code
        for index in range(umbrella_allowed)
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(
            route,
            ip=SOURCE_IP,
            index=umbrella_allowed,
            email="rotated-final@example.invalid",
        ).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )


def test_submitted_email_is_normalized_for_case_and_whitespace() -> None:
    variants = (
        " Victim@Example.COM ",
        "victim@example.com",
        "VICTIM@EXAMPLE.COM",
        " victim@EXAMPLE.com",
        "Victim@example.com ",
    )
    served = [
        _post(
            _LOGIN,
            ip=f"203.0.113.{index + 1}",
            email=email,
        ).status_code
        for index, email in enumerate(variants)
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(_LOGIN, ip=TARGET_IP, email="victim@example.com").status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )


def test_reset_request_response_is_equivalent_for_known_and_unknown_email() -> None:
    known_email = "known@example.com"
    known = User.objects.create_user(
        email=known_email,
        password=PASSWORD,
    )
    EmailAddress.objects.create(
        user=known,
        email=known_email,
        verified=True,
        primary=True,
    )

    known_response = _post(_RESET_REQUEST, ip=SOURCE_IP, email=known_email)
    unknown_response = _post(
        _RESET_REQUEST,
        ip=TARGET_IP,
        email="unknown@example.com",
    )

    assert django_settings.ACCOUNT_PREVENT_ENUMERATION is True
    assert known_response.status_code == unknown_response.status_code
    assert known_response.headers.get("Location") == unknown_response.headers.get(
        "Location",
    )
    assert known_response.content == unknown_response.content


def test_socket_name_resolution_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate_cache = caches[getattr(django_settings, "RATELIMIT_USE_CACHE", "default")]

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        raise socket.gaierror

    monkeypatch.setattr(rate_cache, "add", unavailable)

    assert _post(_LOGIN, ip=SOURCE_IP).status_code == HTTPStatus.TOO_MANY_REQUESTS


def test_non_dns_redis_failure_propagates_as_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate_cache = caches[getattr(django_settings, "RATELIMIT_USE_CACHE", "default")]

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        raise ConnectionError

    monkeypatch.setattr(rate_cache, "add", unavailable)
    client = Client(raise_request_exception=False)
    response = client.post(
        _path(_LOGIN, 0),
        _payload(_LOGIN, 0),
        headers={"host": _LOGIN.host},
        REMOTE_ADDR=SOURCE_IP,
    )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR


def test_endpoint_bucket_accepts_requests_after_its_window_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = [
        _post(_MFA, ip=SOURCE_IP, index=index).status_code
        for index in range(_allowed(_MFA))
    ]
    assert HTTPStatus.TOO_MANY_REQUESTS not in served
    assert (
        _post(_MFA, ip=SOURCE_IP, index=_allowed(_MFA)).status_code
        == HTTPStatus.TOO_MANY_REQUESTS
    )

    next_window = SimpleNamespace(time=lambda: PINNED_INSTANT + 60.0)
    monkeypatch.setattr(ratelimit_core, "time", next_window)

    assert _post(_MFA, ip=SOURCE_IP).status_code != HTTPStatus.TOO_MANY_REQUESTS

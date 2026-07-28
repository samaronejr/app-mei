"""Which address a request is attributed to, given the configured proxy count.

The two failure directions are both silent. Trust the header with no proxy in front
and every per-IP control is decorative; ignore it behind one and every user collapses
into the proxy's bucket, so the first rate-limited request locks out everybody.
"""

import pytest
from django.http import HttpRequest
from django.test import RequestFactory
from pytest_django.fixtures import SettingsWrapper

from apps.core.netaddr import client_ip

CLIENT = "203.0.113.9"
PROXY = "198.51.100.1"
EDGE = "192.0.2.5"


def _request(factory: RequestFactory, forwarded: str | None) -> HttpRequest:
    request = factory.get("/", REMOTE_ADDR=PROXY)
    if forwarded:
        request.META["HTTP_X_FORWARDED_FOR"] = forwarded
    return request


def test_the_header_is_ignored_when_no_proxy_is_declared(
    rf: RequestFactory,
    settings: SettingsWrapper,
) -> None:
    # Given a deployment that controls no proxies
    settings.TRUSTED_PROXY_COUNT = 0

    # When a request arrives carrying a forged forwarded-for header
    request = _request(rf, f"{'evil.spoof'}, {CLIENT}")

    # Then the connection's own address wins. A caller cannot choose their bucket.
    assert client_ip(request) == PROXY


def test_one_declared_proxy_uses_the_entry_that_proxy_appended(
    rf: RequestFactory,
    settings: SettingsWrapper,
) -> None:
    # Given one reverse proxy in front
    settings.TRUSTED_PROXY_COUNT = 1

    # When a request arrives with an attacker-supplied prefix
    request = _request(rf, f"attacker-supplied, {CLIENT}")

    # Then the rightmost entry is used — the only one the proxy actually wrote
    assert client_ip(request) == CLIENT


def test_two_declared_proxies_count_from_the_right(
    rf: RequestFactory,
    settings: SettingsWrapper,
) -> None:
    # Given a CDN in front of a reverse proxy
    settings.TRUSTED_PROXY_COUNT = 2

    # When a request traverses both
    request = _request(rf, f"attacker-supplied, {CLIENT}, {EDGE}")

    # Then the entry two from the right is used
    assert client_ip(request) == CLIENT


def test_a_short_header_falls_back_rather_than_trusting_a_forged_entry(
    rf: RequestFactory,
    settings: SettingsWrapper,
) -> None:
    # Given two declared proxies but a header with only one entry, which means the
    # request did not traverse the expected path
    settings.TRUSTED_PROXY_COUNT = 2

    # When the address is resolved
    request = _request(rf, CLIENT)

    # Then it falls back to REMOTE_ADDR rather than trusting the single entry, which
    # by definition was not written by a proxy we control
    assert client_ip(request) == PROXY


def test_a_missing_header_falls_back_to_the_connection(
    rf: RequestFactory,
    settings: SettingsWrapper,
) -> None:
    # Given a declared proxy that did not set the header
    settings.TRUSTED_PROXY_COUNT = 1

    # When the address is resolved
    # Then the connection's address is used
    assert client_ip(_request(rf, None)) == PROXY


@pytest.mark.parametrize("count", [0, 1, 2])
def test_an_address_is_always_produced(
    count: int, rf: RequestFactory, settings: SettingsWrapper
) -> None:
    # Given any declared proxy count
    settings.TRUSTED_PROXY_COUNT = count

    # When a plain request is resolved
    # Then some address is returned, never None, so no control is silently skipped
    assert client_ip(rf.get("/", REMOTE_ADDR=PROXY)) == PROXY

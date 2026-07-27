"""The settings guards must reject exactly the silent no-ops they exist to catch."""

from django.conf import settings
from django.db import connections
from django.test import override_settings

from apps.core.checks import check_transaction_and_cookie_policy


def _ids() -> set[str | None]:
    return {m.id for m in check_transaction_and_cookie_policy(None)}


def test_atomic_requests_lives_on_the_connection_not_the_settings_module() -> None:
    # Given the configured default database
    connection_value = connections["default"].settings_dict["ATOMIC_REQUESTS"]

    # When the per-database key is read
    # Then it is True — this is the value make_view_atomic actually consults
    assert connection_value is True


def test_guards_pass_under_the_shipped_settings() -> None:
    # Given the settings module under test
    # When every guard runs
    # Then nothing is reported
    assert check_transaction_and_cookie_policy(None) == []


def test_cookie_policy_is_host_only() -> None:
    # Given the shipped settings
    # When the cookie scope is inspected
    # Then neither cookie is pinned to a parent domain and the CSRF token is HttpOnly
    assert settings.SESSION_COOKIE_DOMAIN is None
    assert settings.CSRF_COOKIE_DOMAIN is None
    assert settings.CSRF_COOKIE_HTTPONLY is True


@override_settings(SESSION_COOKIE_DOMAIN=".example.com")
def test_parent_domain_session_cookie_is_rejected() -> None:
    # Given a session cookie scoped to the parent domain
    # When the guards run
    # Then core.E002 fires
    assert "core.E002" in _ids()


@override_settings(CSRF_TRUSTED_ORIGINS=["https://*.example.com"])
def test_wildcard_csrf_origin_is_rejected() -> None:
    # Given a wildcard CSRF origin
    # When the guards run
    # Then core.E003 fires
    assert "core.E003" in _ids()


@override_settings(CSRF_COOKIE_HTTPONLY=False)
def test_javascript_readable_csrf_cookie_is_rejected() -> None:
    # Given a CSRF cookie that JavaScript can read
    # When the guards run
    # Then core.E004 fires
    assert "core.E004" in _ids()

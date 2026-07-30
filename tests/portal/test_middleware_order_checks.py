"""`core.E005/E006/E007` must police BOTH transaction owners, not whichever comes first.

`PortalMiddleware` opens a request-scoped transaction and sets GUCs exactly as
`TenantMiddleware` does, so every ordering rule that protects one protects the other.
Before Wave 3 the checks keyed on the literal `TenantMiddleware` and were blind to it.

The mutation that matters is **removing one owner**, not both. A `min()` over whichever
owners happen to be registered passes that case while the failure is fully reachable:
`TenantMiddleware` no-ops itself on a portal host, so a settings module missing
`PortalMiddleware` serves portal requests with no tenant context, no portal context and
no error at all — `TenantScopedManager` returns `.none()` and the pages are just empty.
That is why each mutation below is named individually rather than counted.
"""

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.checks import (
    ACCESS_LOG_MIDDLEWARE,
    AUTHENTICATION_MIDDLEWARE,
    HOST_DISPATCH_MIDDLEWARE,
    MFA_MIDDLEWARE,
    PORTAL_MIDDLEWARE,
    RATE_LIMIT_MIDDLEWARE,
    TENANT_MIDDLEWARE,
    check_tenant_middleware,
)


def _ids(settings: SettingsWrapper, middleware: list[str]) -> list[str | None]:
    settings.MIDDLEWARE = middleware
    return [message.id for message in check_tenant_middleware(None)]


def test_the_shipped_order_is_clean(settings: SettingsWrapper) -> None:
    # Given the real MIDDLEWARE
    # Then nothing is reported, so every mutation below fails for its own reason
    assert check_tenant_middleware(None) == []
    assert settings.MIDDLEWARE


@pytest.mark.parametrize("absent", [PORTAL_MIDDLEWARE, TENANT_MIDDLEWARE])
def test_e005_fires_when_either_transaction_owner_is_absent(
    settings: SettingsWrapper,
    absent: str,
) -> None:
    # Given a stack missing exactly one owner — the other is still present
    middleware = [m for m in settings.MIDDLEWARE if m != absent]

    # Then E005 fires. A min() over present indices would report nothing here, which is
    # precisely the silent state this check exists to prevent.
    assert "core.E005" in _ids(settings, middleware)


def test_e006_fires_when_an_owner_precedes_authentication(
    settings: SettingsWrapper,
) -> None:
    # Given the portal middleware moved before AuthenticationMiddleware
    middleware = [m for m in settings.MIDDLEWARE if m != PORTAL_MIDDLEWARE]
    middleware.insert(middleware.index(AUTHENTICATION_MIDDLEWARE), PORTAL_MIDDLEWARE)

    # Then E006 fires: request.user would not exist yet, so the membership check would
    # pass for everyone.
    assert "core.E006" in _ids(settings, middleware)


def test_e006_fires_when_the_dispatcher_runs_after_the_portal(
    settings: SettingsWrapper,
) -> None:
    # Given the dispatcher moved after PortalMiddleware
    middleware = [m for m in settings.MIDDLEWARE if m != HOST_DISPATCH_MIDDLEWARE]
    middleware.insert(
        middleware.index(PORTAL_MIDDLEWARE) + 1,
        HOST_DISPATCH_MIDDLEWARE,
    )

    # Then E006 fires. request.urlconf would be unset while both _is_non_atomic
    # implementations resolve against it, deciding transaction behaviour from the wrong
    # URL tree.
    assert "core.E006" in _ids(settings, middleware)


def test_e006_fires_when_the_dispatcher_is_absent(settings: SettingsWrapper) -> None:
    # Given no dispatcher at all
    middleware = [m for m in settings.MIDDLEWARE if m != HOST_DISPATCH_MIDDLEWARE]

    # Then E006 fires: a portal host would be served the firm's URL tree.
    assert "core.E006" in _ids(settings, middleware)


@pytest.mark.parametrize("owner", [PORTAL_MIDDLEWARE, TENANT_MIDDLEWARE])
def test_e007_fires_when_the_access_log_moves_inside_either_transaction(
    settings: SettingsWrapper,
    owner: str,
) -> None:
    # Given the access log moved after one of the owners
    middleware = [m for m in settings.MIDDLEWARE if m != ACCESS_LOG_MIDDLEWARE]
    middleware.insert(middleware.index(owner) + 1, ACCESS_LOG_MIDDLEWARE)

    # Then E007 fires for that owner. Inside a request transaction a rolled-back
    # request erases its own statutory access log — the record an incident actually
    # needs.
    assert "core.E007" in _ids(settings, middleware)


def test_e006_fires_when_the_portal_is_hoisted_outside_the_mfa_middleware(
    settings: SettingsWrapper,
) -> None:
    # Given the portal group moved before MFAEnforcementMiddleware
    middleware = [
        m
        for m in settings.MIDDLEWARE
        if m not in {HOST_DISPATCH_MIDDLEWARE, PORTAL_MIDDLEWARE}
    ]
    at = middleware.index(MFA_MIDDLEWARE)
    middleware[at:at] = [HOST_DISPATCH_MIDDLEWARE, PORTAL_MIDDLEWARE]

    # Then E006 fires. Outside the MFA middleware, _must_enrol runs INSIDE the portal
    # transaction and reads tenants_membership and mfa_authenticator — app_portal can
    # see neither, so every authenticated portal request 500s. The three-way check this
    # replaced reported nothing for this mutation.
    assert "core.E006" in _ids(settings, middleware)


def test_e006_fires_when_the_rate_limiter_is_hoisted_above_the_group(
    settings: SettingsWrapper,
) -> None:
    # Given RateLimitMiddleware moved above the whole portal group
    middleware = [m for m in settings.MIDDLEWARE if m != RATE_LIMIT_MIDDLEWARE]
    middleware.insert(middleware.index(MFA_MIDDLEWARE), RATE_LIMIT_MIDDLEWARE)

    # Then E006 fires. This is the fourth leg of the positional assertion, which
    # nothing enforced before: the previous check passed and the whole suite passed.
    assert "core.E006" in _ids(settings, middleware)

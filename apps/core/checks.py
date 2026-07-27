"""Settings guards for policies that fail silently rather than loudly.

Each check here covers a setting whose wrong value produces no error, no warning and
no visible symptom until a tenant boundary or a transaction boundary has already been
crossed. `manage.py check` runs them; CI runs it once per settings module.
"""

from collections.abc import Sequence
from typing import Any

from django.apps.config import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error
from django.db import connections


def _atomic_requests_errors() -> list[CheckMessage]:
    value = connections["default"].settings_dict.get("ATOMIC_REQUESTS")
    if value is True:
        return []
    return [
        Error(
            f"DATABASES['default']['ATOMIC_REQUESTS'] is not True (got {value!r}).",
            hint=(
                "ATOMIC_REQUESTS is a per-database key, not a module-level setting. "
                "A module-level assignment makes settings.ATOMIC_REQUESTS report True "
                "while the connection keeps False, so no view is ever wrapped in a "
                "transaction. Set DATABASES['default']['ATOMIC_REQUESTS'] = True, and "
                "mutate individual DATABASES keys in dev/prod/test rather than "
                "reassigning the dict."
            ),
            id="core.E001",
        ),
    ]


def _cookie_domain_errors() -> list[CheckMessage]:
    hint = (
        "This product gives one subdomain to each mutually-untrusted accounting firm. "
        "A parent-domain cookie lets any *.example.com origin set a sessionid for the "
        "whole domain, and RFC 6265 gives Django no way to tell which host set it."
    )
    return [
        Error(
            f"{name} must be None so cookies stay host-only.",
            hint=hint,
            id="core.E002",
        )
        for name in ("SESSION_COOKIE_DOMAIN", "CSRF_COOKIE_DOMAIN")
        if getattr(settings, name) is not None
    ]


def _csrf_origin_errors() -> list[CheckMessage]:
    offenders = [o for o in settings.CSRF_TRUSTED_ORIGINS if "*" in o]
    if not offenders:
        return []
    return [
        Error(
            f"CSRF_TRUSTED_ORIGINS contains wildcard entries: {offenders!r}.",
            hint=(
                "List every trusted origin explicitly. A wildcard re-admits the "
                "sibling-subdomain origins that host-only cookies exist to exclude."
            ),
            id="core.E003",
        ),
    ]


def _csrf_httponly_errors() -> list[CheckMessage]:
    if settings.CSRF_COOKIE_HTTPONLY:
        return []
    return [
        Error(
            "CSRF_COOKIE_HTTPONLY must be True.",
            hint=(
                "Templates read the token from {% csrf_token %}, so JavaScript never "
                "needs the cookie. Leaving it readable hands the token to any XSS on "
                "a sibling subdomain."
            ),
            id="core.E004",
        ),
    ]


TENANT_MIDDLEWARE = "apps.tenants.middleware.TenantMiddleware"
AUTHENTICATION_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"


def _tenant_middleware_errors() -> list[CheckMessage]:
    middleware = list(settings.MIDDLEWARE)
    if TENANT_MIDDLEWARE not in middleware:
        return [
            Error(
                f"{TENANT_MIDDLEWARE} is missing from MIDDLEWARE.",
                hint=(
                    "Without it no request ever sets app.tenant_id, so every "
                    "row-level-security policy denies every row and every scoped "
                    "queryset is empty. Register it after AuthenticationMiddleware."
                ),
                id="core.E005",
            ),
        ]
    if AUTHENTICATION_MIDDLEWARE not in middleware:
        return []
    if middleware.index(TENANT_MIDDLEWARE) < middleware.index(
        AUTHENTICATION_MIDDLEWARE
    ):
        return [
            Error(
                f"{TENANT_MIDDLEWARE} runs before AuthenticationMiddleware.",
                hint=(
                    "Tenant resolution reads request.user to check membership. Run "
                    "before authentication and request.user does not exist yet, so "
                    "the membership check silently passes for everyone."
                ),
                id="core.E006",
            ),
        ]
    return []


def check_tenant_middleware(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Reject a middleware stack that would leave tenant context unset."""
    return _tenant_middleware_errors()


def check_transaction_and_cookie_policy(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Reject transaction and cookie settings that would be silent no-ops."""
    return [
        *_atomic_requests_errors(),
        *_cookie_domain_errors(),
        *_csrf_origin_errors(),
        *_csrf_httponly_errors(),
    ]

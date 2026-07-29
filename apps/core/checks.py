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
from django.db import DatabaseError, connections


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


RUNTIME_ROLE = "app_runtime"
PORTAL_ROLE = "app_portal"

TENANT_MIDDLEWARE = "apps.tenants.middleware.TenantMiddleware"
AUTHENTICATION_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"
ACCESS_LOG_MIDDLEWARE = "apps.audit.middleware.AccessLogMiddleware"
PLATFORM_EVENT_MIDDLEWARE = "apps.audit.middleware.PlatformEventMiddleware"


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


def _outside_tenant_transaction_errors() -> list[CheckMessage]:
    """Both audit middlewares must sit OUTSIDE the tenant transaction.

    Registered after `TenantMiddleware` they would run inside its `atomic()` block,
    and the rollback on a failed request would erase the two records an incident
    investigation actually needs: the statutory access log, and the audit event for
    the action that was denied. Nothing else reports that — the writes appear to
    succeed and the rows are simply absent afterwards.
    """
    middleware = list(settings.MIDDLEWARE)
    if TENANT_MIDDLEWARE not in middleware:
        return []
    tenant_index = middleware.index(TENANT_MIDDLEWARE)
    return [
        Error(
            f"{name} must be registered before {TENANT_MIDDLEWARE}.",
            hint=(
                "Inside the tenant transaction, a rolled-back request erases its own "
                "audit and access-log rows — precisely the records that matter when a "
                "request fails. List it earlier in MIDDLEWARE."
            ),
            id="core.E007",
        )
        for name in (ACCESS_LOG_MIDDLEWARE, PLATFORM_EVENT_MIDDLEWARE)
        if name in middleware and middleware.index(name) > tenant_index
    ]


def _portal_role_errors() -> list[CheckMessage]:
    """Require the portal role to exist and not be inherited by the runtime role.

    Both failures are silent in opposite directions. A missing `app_portal` means
    `SET LOCAL ROLE` raises on the first portal request. Worse, an inheriting grant
    means PostgreSQL applies the RESTRICTIVE portal policies to `app_runtime` as well —
    it matches a policy's `TO` clause by privilege inheritance, not identity — and the
    accounting firm reads zero rows from every client table with no error anywhere.

    This is the only check here that queries the database. It returns cleanly when the
    database is unreachable so `manage.py` stays usable without one; CI runs `check`
    against a live, role-bootstrapped cluster, which is where it bites.
    """
    hint = (
        "Run ops/sql/roles.sql as the bootstrap superuser. The grant must be "
        "'GRANT app_portal TO app_runtime WITH INHERIT FALSE, SET TRUE'. A plain "
        "re-GRANT of an existing membership is a no-op: correcting it needs an "
        "explicit WITH INHERIT FALSE, or a REVOKE followed by a GRANT."
    )
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                [PORTAL_ROLE],
            )
            exists_row = cursor.fetchone()
            if not (exists_row and exists_row[0]):
                return [
                    Error(
                        f"The {PORTAL_ROLE} database role does not exist.",
                        hint=hint,
                        id="core.E008",
                    ),
                ]
            cursor.execute(
                "SELECT pg_has_role(%s, %s, 'USAGE')",
                [RUNTIME_ROLE, PORTAL_ROLE],
            )
            inherits_row = cursor.fetchone()
    except DatabaseError:
        return []
    if inherits_row and inherits_row[0]:
        return [
            Error(
                f"{RUNTIME_ROLE} INHERITS {PORTAL_ROLE}, so every restrictive portal "
                f"policy also binds {RUNTIME_ROLE}.",
                hint=hint,
                id="core.E008",
            ),
        ]
    return []


def check_portal_role(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Reject a cluster where the portal role is absent or wrongly inherited."""
    return _portal_role_errors()


def check_tenant_middleware(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Reject a middleware stack that would leave tenant context unset."""
    return [*_tenant_middleware_errors(), *_outside_tenant_transaction_errors()]


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

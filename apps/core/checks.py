"""Settings guards for policies that fail silently rather than loudly.

Each check here covers a setting whose wrong value produces no error, no warning and
no visible symptom until a tenant boundary or a transaction boundary has already been
crossed. `manage.py check` runs them; CI runs it once per settings module.
"""

from collections.abc import Callable, Sequence
from typing import Any

from django.apps import apps as django_apps
from django.apps.config import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, connections
from django.urls import get_resolver

from apps.tenants.validators import validate_tenant_slug


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
PORTAL_MIDDLEWARE = "apps.portal.middleware.PortalMiddleware"
HOST_DISPATCH_MIDDLEWARE = "apps.portal.middleware.HostDispatchMiddleware"
MFA_MIDDLEWARE = "apps.accounts.middleware.MFAEnforcementMiddleware"
RATE_LIMIT_MIDDLEWARE = "apps.security.middleware.RateLimitMiddleware"
AUTHENTICATION_MIDDLEWARE = "django.contrib.auth.middleware.AuthenticationMiddleware"
ACCESS_LOG_MIDDLEWARE = "apps.audit.middleware.AccessLogMiddleware"
PLATFORM_EVENT_MIDDLEWARE = "apps.audit.middleware.PlatformEventMiddleware"

# Both middlewares that open a request-scoped transaction and set a GUC. Every ordering
# rule below applies to EACH of them, not to whichever happens to appear first.
TRANSACTION_MIDDLEWARE: tuple[str, ...] = (TENANT_MIDDLEWARE, PORTAL_MIDDLEWARE)


def _tenant_middleware_errors() -> list[CheckMessage]:
    """Require both transaction owners to be present and correctly ordered.

    Deliberately NOT `min()` over whichever owners happen to be registered. That
    formulation only fires when BOTH are absent, and the reachable failure is one of
    them missing: `TenantMiddleware` no-ops itself on a portal host, so a settings
    module that omits `PortalMiddleware` serves portal requests with no tenant context,
    no portal context and **no error** — `TenantScopedManager` returns `.none()` and the
    pages are simply empty.
    """
    middleware = list(settings.MIDDLEWARE)
    missing = [name for name in TRANSACTION_MIDDLEWARE if name not in middleware]
    if missing:
        return [
            Error(
                f"{name} is missing from MIDDLEWARE.",
                hint=(
                    "Without it no request ever sets its GUC, so every "
                    "row-level-security policy denies every row and every scoped "
                    "queryset is empty — silently. Register it after "
                    "AuthenticationMiddleware."
                ),
                id="core.E005",
            )
            for name in missing
        ]
    if AUTHENTICATION_MIDDLEWARE not in middleware:
        return []
    authentication_index = middleware.index(AUTHENTICATION_MIDDLEWARE)
    errors = [
        Error(
            f"{name} runs before AuthenticationMiddleware.",
            hint=(
                "Context resolution reads request.user to check membership. Run "
                "before authentication and request.user does not exist yet, so "
                "the membership check silently passes for everyone."
            ),
            id="core.E006",
        )
        for name in TRANSACTION_MIDDLEWARE
        if middleware.index(name) < authentication_index
    ]
    return [*errors, *_portal_dispatch_order_errors(middleware)]


def _portal_dispatch_order_errors(middleware: list[str]) -> list[CheckMessage]:
    """Require dispatcher before portal before tenant."""
    if HOST_DISPATCH_MIDDLEWARE not in middleware:
        return [
            Error(
                f"{HOST_DISPATCH_MIDDLEWARE} is missing from MIDDLEWARE.",
                hint=(
                    "Without it request.urlconf is never set, so a portal host is "
                    "served the firm's URL tree."
                ),
                id="core.E006",
            ),
        ]
    # The full four-way order, anchored on MFAEnforcementMiddleware. An earlier version
    # asserted only that the first three were in relative order, which left two legs
    # unenforced: RateLimitMiddleware could be hoisted above the whole group, and the
    # portal could be moved OUTSIDE the MFA middleware, and `manage.py check` stayed
    # clean for both. The second is the dangerous one — outside MFA, `_must_enrol` runs
    # inside the portal transaction and reads tenants_membership and mfa_authenticator,
    # neither of which app_portal may see, so every authenticated portal request 500s.
    ordered = (
        MFA_MIDDLEWARE,
        HOST_DISPATCH_MIDDLEWARE,
        PORTAL_MIDDLEWARE,
        TENANT_MIDDLEWARE,
        RATE_LIMIT_MIDDLEWARE,
    )
    present = [name for name in ordered if name in middleware]
    indices = [middleware.index(name) for name in present]
    if indices == sorted(indices):
        return []
    return [
        Error(
            "Portal middleware order is wrong. Required: "
            + " -> ".join(name.rsplit(".", 1)[-1] for name in ordered),
            hint=(
                "The dispatcher sets request.urlconf, which both _is_non_atomic "
                "implementations resolve against. The portal must sit INSIDE "
                "MFAEnforcementMiddleware, or _must_enrol runs in the portal "
                "transaction and reads tables app_portal cannot see. The tenant "
                "middleware no-ops itself on a portal host only after the portal has "
                "established context, and the rate limiter keys on a resolved tenant."
            ),
            id="core.E006",
        ),
    ]


def _outside_tenant_transaction_errors() -> list[CheckMessage]:
    """Both audit middlewares must sit OUTSIDE the tenant transaction.

    Registered after `TenantMiddleware` they would run inside its `atomic()` block,
    and the rollback on a failed request would erase the two records an incident
    investigation actually needs: the statutory access log, and the audit event for
    the action that was denied. Nothing else reports that — the writes appear to
    succeed and the rows are simply absent afterwards.
    """
    middleware = list(settings.MIDDLEWARE)
    owners = [name for name in TRANSACTION_MIDDLEWARE if name in middleware]
    if not owners:
        return []
    return [
        Error(
            f"{name} must be registered before {owner}.",
            hint=(
                "Inside a request transaction, a rolled-back request erases its own "
                "audit and access-log rows — precisely the records that matter when a "
                "request fails. List it earlier in MIDDLEWARE."
            ),
            id="core.E007",
        )
        for owner in owners
        for name in (ACCESS_LOG_MIDDLEWARE, PLATFORM_EVENT_MIDDLEWARE)
        if name in middleware and middleware.index(name) > middleware.index(owner)
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


def _tenant_slug_errors() -> list[CheckMessage]:
    """Report stored slugs the portal host scheme cannot safely derive a host from.

    The validator on `Tenant.slug` only guards rows written through a form or a
    `full_clean()`. Slugs predating it, and any created by a bulk path that skips
    validation, are still in the table — and this is the one control that sees them.
    """
    tenant_model = django_apps.get_model("tenants", "Tenant")
    try:
        # PLATFORM_QUERY_OK: a boot-time audit of the tenancy root itself, which has no
        # tenant to be scoped to. Reads slugs only.
        slugs = list(tenant_model.objects.values_list("slug", flat=True))
    except DatabaseError:
        return []

    offenders = []
    for slug in slugs:
        try:
            validate_tenant_slug(str(slug))
        except ValidationError as invalid:
            offenders.append(f"{slug} ({'; '.join(invalid.messages)})")

    if not offenders:
        return []
    return [
        Error(
            "Stored tenant slugs are incompatible with the portal host scheme: "
            + ", ".join(sorted(offenders)),
            hint=(
                "The portal is served at <slug>-portal.<domain>. A slug ending in "
                "-portal takes over another firm's portal host, and host dispatch "
                "would route this firm's own front door to the portal urlconf. "
                "Rename the firm before the portal ships."
            ),
            id="core.E009",
        ),
    ]


def check_tenant_slugs(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Reject stored slugs that collide with a portal host or overflow a DNS label."""
    return _tenant_slug_errors()


def check_tenant_middleware(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Reject a middleware stack that would leave tenant context unset."""
    return [*_tenant_middleware_errors(), *_outside_tenant_transaction_errors()]


def _portal_capability_gate_errors() -> list[CheckMessage]:
    """Refuse a portal view gated on a capability that cannot admit its own users.

    `require_can` calls `can(request.user, action)` with no object, and both
    object-refined levels return False when the object is None. So a portal view gated
    at anything below FULL is an unconditional 403 for the roles that hold it there —
    silently, and only for those roles, which is why it survives testing as an owner.

    Pure Python and no database read, deliberately. `MATRIX` is a module literal, and
    `Membership`'s role-scope CHECK constraint plus `PortalMiddleware`'s
    `client__isnull=False` filter mean a portal request can only ever carry the two
    client roles — so which columns to assert is known statically.
    """
    from apps.authz.matrix import MATRIX, ROLE_ORDER  # noqa: PLC0415
    from apps.authz.models import GrantLevel  # noqa: PLC0415
    from apps.authz.services import PORTAL_CAPABILITY_GATES  # noqa: PLC0415
    from apps.portal.middleware import PORTAL_URLCONF  # noqa: PLC0415

    portal_roles = ("client_owner", "client_collaborator")
    try:
        columns = [ROLE_ORDER.index(role) for role in portal_roles]
    except ValueError:  # pragma: no cover - ROLE_ORDER is a literal
        return []
    levels = {row.slug: row.levels for row in MATRIX}

    offenders: list[str] = []
    for callback in _portal_callbacks(PORTAL_URLCONF):
        action = PORTAL_CAPABILITY_GATES.get(callback)
        if action is None:
            continue
        row = levels.get(action)
        if row is None:
            offenders.append(f"{callback.__qualname__} gates on unknown {action!r}")
            continue
        for role, column in zip(portal_roles, columns, strict=True):
            level = row[column]
            if level != GrantLevel.FULL:
                offenders.append(
                    f"{callback.__qualname__} gates on {action!r}, which is "
                    f"{level} for {role}",
                )

    if not offenders:
        return []
    return [
        Error(
            "Portal views are gated on capabilities that refuse their own users: "
            + "; ".join(sorted(offenders)),
            hint=(
                "require_can passes no object, so below FULL the gate is an "
                "unconditional 403 for that role. Gate the view on a capability that "
                "is FULL for both client roles, or authorise it some other way."
            ),
            id="core.E010",
        ),
    ]


def _portal_callbacks(urlconf: str) -> list[Callable[..., Any]]:
    """Return every view reachable from the portal urlconf, resolvers included."""
    try:
        resolver = get_resolver(urlconf)
        patterns = list(resolver.url_patterns)
    except (ImproperlyConfigured, ImportError, AttributeError):
        return []

    found: list[Callable[..., Any]] = []
    while patterns:
        entry = patterns.pop()
        nested = getattr(entry, "url_patterns", None)
        if nested is not None:
            patterns.extend(nested)
            continue
        callback = getattr(entry, "callback", None)
        if callable(callback):
            found.append(callback)
    return found


def check_portal_capability_gates(
    app_configs: Sequence[AppConfig] | None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> list[CheckMessage]:
    """Reject a portal view whose capability gate would refuse every holder."""
    return _portal_capability_gate_errors()


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

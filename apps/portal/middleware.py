"""Run a portal request as `app_portal`, confined to exactly one client.

This is the request-path half of the portal isolation design. Wave 1 put RESTRICTIVE
policies `TO app_portal` on every client-scoped table and granted the role to
`app_runtime` `WITH INHERIT FALSE, SET TRUE`; Wave 2 taught `role_of()` to filter on a
client context. Neither does anything until something assumes the role and sets the
GUC on a real request. That is this module.

`PortalMiddleware` copies `TenantMiddleware`'s structure deliberately — the streaming
guard, the render-inside-transaction guard and the token/reset discipline are all load
bearing for the same reasons documented there, and a portal request needs every one of
them. What it adds is the role switch and the client dimension.

Four things here are easy to get wrong and silent when wrong:

* **`SET LOCAL` outside a transaction emits a warning and does nothing** (verified on
  PostgreSQL 16.14). Were the role switch to land outside the `atomic()` block, the
  request would run as `app_runtime` — for whom the RESTRICTIVE policies do not bind,
  because the grant is non-inheriting — with `app.client_id` set and nothing appearing
  wrong. A portal user would read the firm's entire client base. The role is therefore
  switched *inside* the block and then **asserted positively**, never inferred from the
  absence of an error.
* **The client identity comes from the membership row and nowhere else.** Never from
  the host, a query parameter, a header or the session. `_grant_context` returns the
  `Membership`, and the GUC is written from `membership.client_id`.
* **`AdminTenantMiddleware` is inert on this host.** Its own lookup uses
  `slug_from_host`, which yields the literal `"acme-portal"` — a slug the reserved-slug
  validator guarantees no tenant owns — so its 404 never fires here. The admin refusal
  below is the *only* control keeping the Django admin off the portal host, not a
  second layer. Deleting it as redundant would open it.
* **The role must not leak.** `SET LOCAL ROLE` reverts at `COMMIT` and at `ROLLBACK`,
  but *merges upward* on `RELEASE SAVEPOINT`. This middleware therefore owns the OUTER
  transaction; nesting it inside another would leak `app_portal` into the enclosing
  block. gunicorn runs `sync --threads 1` with `CONN_MAX_AGE` set, so one connection is
  reused across requests and a leak would be cross-request.
"""

from collections.abc import Callable

from django.core.exceptions import PermissionDenied
from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.http import Http404, HttpRequest, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.urls import Resolver404, resolve

from apps.core.tenancy import (
    CLIENT_GUC,
    NO_CLIENT,
    NO_TENANT,
    TENANT_GUC,
    current_client_id,
    current_tenant_id,
)
from apps.portal.hosts import portal_slug_from_host
from apps.tenants.models import Membership, Tenant

PORTAL_ROLE = "app_portal"
ADMIN_PREFIX = "/admin/"
PORTAL_URLCONF = "apps.portal.urls"

# Account management runs WITHOUT the portal role, in the same shape as an anonymous
# request: both GUCs empty, still app_runtime.
#
# Not a convenience. allauth's own tables — account_emailaddress, mfa_authenticator,
# django_session — are none of them among the six app_portal may read, and the role
# holds no write privilege anywhere at all. Run this tree inside the portal role and
# every one of these is a 500:
#
#   * MFAEnforcementMiddleware redirects EVERY unenrolled portal user to
#     /accounts/2fa/totp/activate/, which reads account_emailaddress. That is the first
#     request a new MEI owner makes, so nobody can ever enrol — the portal cannot
#     onboard a single user.
#   * Logging out deletes a django_session row.
#   * Changing a password does both.
#
# Nothing under this prefix needs client-scoped data, so withholding the client context
# costs nothing and restores the whole flow. `is_exempt_path` in apps/accounts/mfa.py
# already exempts the same prefix for the same underlying reason.
ACCOUNTS_PREFIX = "/accounts/"


class PortalHttpRequest(HttpRequest):
    """An `HttpRequest` after this middleware has attached the tenant and client."""

    tenant: Tenant | None
    client: object | None


class DispatchableHttpRequest(HttpRequest):
    """An `HttpRequest` whose urlconf the dispatcher may reassign.

    Django reads `request.urlconf` in `BaseHandler._get_response`, but it is not
    declared on `HttpRequest`, so it is named here rather than assigned dynamically.
    """

    urlconf: str


class HostDispatchMiddleware:
    """Select the urlconf by host: a portal host gets the portal URL tree.

    Registered before `PortalMiddleware` because `_is_non_atomic` in both this app's
    middleware and `TenantMiddleware` resolves against `request.urlconf`. A dispatcher
    running later would have them decide transaction behaviour from `ROOT_URLCONF`,
    which is the wrong tree for a portal-only path.

    It does NOT need to precede `RateLimitMiddleware` for the rate limit to work: the
    portal urlconf mounts allauth at the same paths and names, so `resolve()` returns
    `account_login` either way. That was a stated justification in an earlier revision
    of the plan and it does not hold.
    """

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: DispatchableHttpRequest) -> HttpResponseBase:
        """Point the request at the portal urlconf when the host is a portal host."""
        if portal_slug_from_host(request.get_host()) is not None:
            request.urlconf = PORTAL_URLCONF
        return self.get_response(request)


class PortalMiddleware:
    """Assume `app_portal` and pin one client for the whole of a portal request."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: PortalHttpRequest) -> HttpResponseBase:
        """Establish portal context, run the request inside it, and tear it down."""
        slug = portal_slug_from_host(request.get_host())
        if slug is None:
            return self.get_response(request)
        if request.path_info.startswith(ADMIN_PREFIX):
            # Before the transaction and before the role switch. AdminTenantMiddleware
            # cannot refuse this host (see the module docstring), and reaching it would
            # query tenants_tenant as app_portal and raise permission denied — a 500
            # where the firm host answers 404.
            raise Http404
        if self._is_non_atomic(request):
            return self._run_without_database_context(request)
        tenant = self._resolve_tenant(slug)
        membership = (
            None
            if request.path_info.startswith(ACCOUNTS_PREFIX)
            else self._grant_context(request, tenant)
        )
        # Resolving the tenant from the host is ROUTING; granting context is
        # AUTHORIZATION. An anonymous request gets neither dimension — exactly as
        # TenantMiddleware already does firm-side — so the login page renders with both
        # GUCs empty. Handing an anonymous visitor `app.tenant_id` because the hostname
        # named a firm would let that page read the firm's tenant-scoped rows as
        # app_runtime, which is a widening nobody asked for.
        granted = tenant if membership is not None else None
        return self._run_in_portal_context(request, granted, membership)

    @staticmethod
    def _is_non_atomic(request: HttpRequest) -> bool:
        """Report whether the target view opted out of request transactions."""
        try:
            match = resolve(
                request.path_info,
                urlconf=getattr(request, "urlconf", None),
            )
        except Resolver404:
            return False
        return DEFAULT_DB_ALIAS in getattr(match.func, "_non_atomic_requests", set())

    def _run_without_database_context(
        self,
        request: PortalHttpRequest,
    ) -> HttpResponseBase:
        """Serve a database-free view with neither dimension established."""
        request.tenant = None
        request.client = None
        tenant_token = current_tenant_id.set(None)
        client_token = current_client_id.set(None)
        try:
            return self.get_response(request)
        finally:
            current_client_id.reset(client_token)
            current_tenant_id.reset(tenant_token)

    @staticmethod
    def _resolve_tenant(slug: str) -> Tenant | None:
        # PLATFORM_QUERY_OK: the bootstrap lookup, as app_runtime and before any GUC
        # exists. Resolving which firm a hostname names precedes all context.
        return Tenant.objects.filter(slug=slug, is_active=True).first()

    @staticmethod
    def _grant_context(
        request: HttpRequest,
        tenant: Tenant | None,
    ) -> Membership | None:
        """Return the client-role membership this request may operate as, if any.

        Anonymous requests get `None` and run as `app_runtime` with both GUCs empty, so
        the login page renders. Requiring a membership here instead would 403 the very
        page a portal user needs in order to obtain one.
        """
        if tenant is None:
            return None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        # PLATFORM_QUERY_OK: the portal's own bootstrap read, the mirror of
        # TenantMiddleware's. It runs as app_runtime before SET LOCAL ROLE, which is
        # why tenants_membership carries no portal policy and app_portal holds no
        # SELECT on it.
        # client__isnull=False is the SECURITY control here, the inverse of the firm
        # side's: a firm-side membership must never satisfy a PORTAL request, or an
        # accountant browsing the portal host would be handed app_portal with their own
        # firm-side row and no client to confine them.
        candidates = Membership.objects.filter(
            user=user,
            tenant=tenant,
            is_active=True,
            client__isnull=False,
        )
        # Ordered so a user holding two client roles in one firm resolves the same
        # client on every request; .first() on an unordered queryset picks arbitrarily.
        membership = candidates.order_by("client_id").first()
        if membership is None:
            msg = "This account has no active client membership for this firm."
            raise PermissionDenied(msg)
        return membership

    def _run_in_portal_context(
        self,
        request: PortalHttpRequest,
        tenant: Tenant | None,
        membership: Membership | None,
    ) -> HttpResponseBase:
        request.tenant = tenant
        client_id = membership.client_id if membership else None
        # token/reset, never a bare set(): one thread serves many requests.
        tenant_token = current_tenant_id.set(tenant.id if tenant else None)
        client_token = current_client_id.set(client_id)
        try:
            with transaction.atomic():
                # Order is forced, not stylistic. The GUCs go first because
                # `membership.client` is a lazy fetch of a tenant-scoped row: read it
                # before `app.tenant_id` exists and the Phase-1 permissive policy
                # returns nothing, raising DoesNotExist. The role switch goes LAST
                # because that same fetch is denied outright to `app_portal`, which
                # holds no SELECT on the row until `app.client_id` is what confines it.
                self._apply_gucs(tenant, client_id)
                request.client = membership.client if membership else None
                if membership is not None:
                    self._assume_portal_role()
                response = self.get_response(request)
                self._reject_streaming(response, membership)
                self._render_inside_transaction(response)
            return response
        finally:
            current_client_id.reset(client_token)
            current_tenant_id.reset(tenant_token)

    @staticmethod
    def _assume_portal_role() -> None:
        """Switch to `app_portal` and prove the switch took effect.

        `SET LOCAL` takes no placeholders, so the role name is interpolated — it is a
        module constant and never request data. Outside a transaction the statement
        warns and does nothing, which would leave the request as `app_runtime` and
        unconfined, so the effective role is read back rather than assumed.
        """
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {PORTAL_ROLE}")
            cursor.execute("SELECT current_user")
            row = cursor.fetchone()
        effective = str(row[0]) if row else None
        if effective != PORTAL_ROLE:
            msg = (
                f"SET LOCAL ROLE {PORTAL_ROLE} did not take effect: current_user is "
                f"{effective!r}. The request would run unconfined, because the "
                f"restrictive portal policies do not bind app_runtime."
            )
            raise RuntimeError(msg)

    @staticmethod
    def _apply_gucs(tenant: Tenant | None, client_id: object | None) -> None:
        # Bound parameters, never interpolation. `true` makes both settings
        # transaction-local so neither can survive on a pooled connection.
        tenant_value = str(tenant.id) if tenant else NO_TENANT
        client_value = str(client_id) if client_id else NO_CLIENT
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                [TENANT_GUC, tenant_value],
            )
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                [CLIENT_GUC, client_value],
            )

    @staticmethod
    def _reject_streaming(
        response: HttpResponseBase,
        membership: Membership | None,
    ) -> None:
        if membership is None or not isinstance(response, StreamingHttpResponse):
            return
        msg = (
            "A portal view returned a streaming response. Its iterator would be "
            "consumed outside the portal transaction, after the role and both GUCs "
            "are gone, and yield zero rows. Build the payload in memory and return "
            "an HttpResponse instead."
        )
        raise TypeError(msg)

    @staticmethod
    def _render_inside_transaction(response: HttpResponseBase) -> None:
        render = getattr(response, "render", None)
        if callable(render) and not getattr(response, "is_rendered", True):
            render()

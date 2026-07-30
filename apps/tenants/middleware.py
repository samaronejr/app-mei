"""Resolve the tenant and hold its context for the whole request.

The transaction is owned HERE, not delegated to `ATOMIC_REQUESTS`, and the two nest
deliberately because each fixes what the other cannot:

* `ATOMIC_REQUESTS` wraps only the **view callable**. A `set_config` issued from
  `process_request` would land in its own autocommit transaction and be discarded
  before the view ran, and template rendering — which happens in the response phase —
  would evaluate lazy querysets with no tenant context and render **zero rows**. Not
  an error: a silently empty page.
* A middleware-owned transaction alone loses rollback-on-exception, because Django's
  `convert_exception_to_response` runs *inside* this block and turns a view exception
  into a response that returns normally. `atomic()` then sees a clean exit and commits
  the partial writes. Testing `status_code >= 500` does not help either: `Http404`,
  `PermissionDenied`, `SuspiciousOperation` and `BadRequest` all convert to 4xx.

So: this middleware opens the OUTER transaction that carries the GUC across the view
*and* rendering, while `ATOMIC_REQUESTS=True` makes the view a nested savepoint that
rolls back on any view exception whatever status it converts to. The GUC is set on the
outer transaction before that savepoint, so a savepoint rollback never disturbs it.
Both are load-bearing, and `core.E001`/`core.E005` fail startup if either is missing.
"""

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.http import HttpRequest, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.urls import Resolver404, resolve

from apps.core.tenancy import current_tenant_id
from apps.portal.hosts import portal_slug_from_host
from apps.tenants.models import Membership, Tenant

TENANT_GUC = "app.tenant_id"
NO_TENANT = ""
MIN_LABELS_FOR_A_SUBDOMAIN = 2


def slug_from_host(host: str) -> str | None:
    """Return the leading label of a hostname, which names the firm."""
    hostname = host.split(":", 1)[0]
    labels = hostname.split(".")
    if len(labels) < MIN_LABELS_FOR_A_SUBDOMAIN:
        return None
    return labels[0] or None


class TenantHttpRequest(HttpRequest):
    """An `HttpRequest` after this middleware has attached the resolved tenant."""

    tenant: Tenant | None


class TenantMiddleware:
    """Resolve the tenant from the subdomain and pin it for the request's lifetime."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: TenantHttpRequest) -> HttpResponseBase:
        """Establish tenant context, run the request inside it, and tear it down."""
        if self._is_non_atomic(request):
            return self._run_without_database_context(request)
        if portal_slug_from_host(request.get_host()) is not None:
            # PortalMiddleware owns this request entirely. Touch neither the ContextVar
            # nor the database.
            #
            # Placed AFTER the _is_non_atomic guard, never before it: get_host() raises
            # DisallowedHost, and /healthz is @non_atomic_requests and must keep serving
            # on hosts outside ALLOWED_HOSTS without opening a connection.
            #
            # Returning early rather than falling through is the whole point.
            # _run_without_database_context sets request.tenant to None and clears
            # current_tenant_id, clobbering what PortalMiddleware just established; and
            # _run_in_tenant_context would call _apply_guc(None) inside a SAVEPOINT of
            # the portal transaction, where set_config(..., true) MERGES UPWARD on
            # RELEASE. app.tenant_id would read empty for the rest of the request and
            # the Phase-1 permissive policy would return zero rows in silence.
            return self.get_response(request)
        resolved = self._resolve_tenant(request)
        granted = self._grant_context(request, resolved)
        return self._run_in_tenant_context(request, granted)

    @staticmethod
    def _is_non_atomic(request: HttpRequest) -> bool:
        """Report whether the target view opted out of request transactions.

        `@transaction.non_atomic_requests` is how a view declares it must not touch
        the database — the liveness probe uses it so that a database blip cannot make
        an orchestrator restart a healthy web process. Opening a transaction here
        would reintroduce exactly the connection that decorator exists to avoid, so
        the same marker Django's `make_view_atomic` consults is honoured here.
        """
        try:
            match = resolve(
                request.path_info, urlconf=getattr(request, "urlconf", None)
            )
        except Resolver404:
            return False
        return DEFAULT_DB_ALIAS in getattr(match.func, "_non_atomic_requests", set())

    def _run_without_database_context(
        self,
        request: TenantHttpRequest,
    ) -> HttpResponseBase:
        """Serve a database-free view with no tenant context at all.

        Any scoped queryset reached from here returns nothing, which is the same
        fail-closed answer the row-level-security policy would give.
        """
        request.tenant = None
        token = current_tenant_id.set(None)
        try:
            return self.get_response(request)
        finally:
            current_tenant_id.reset(token)

    def _resolve_tenant(self, request: HttpRequest) -> Tenant | None:
        slug = slug_from_host(request.get_host())
        if slug is None and settings.DEBUG:
            slug = (
                request.session.get("tenant_slug")
                if hasattr(request, "session")
                else None
            )
        if not slug:
            return None
        # PLATFORM_QUERY_OK: the bootstrap lookup. Resolving which firm a hostname
        # names must happen before any user or tenant context exists; granting
        # context is a separate step below and IS authorized.
        return Tenant.objects.filter(slug=slug, is_active=True).first()

    @staticmethod
    def _grant_context(request: HttpRequest, resolved: Tenant | None) -> Tenant | None:
        """Decide which tenant, if any, this request may actually operate as.

        Resolving a tenant from the hostname is routing; granting context is
        authorization. They are separated because an anonymous caller must be able to
        reach a firm's login page without being handed that firm's data at the
        database layer.
        """
        if resolved is None:
            return None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        # The bootstrap read. Membership carries no app.tenant_id policy precisely so
        # that this can run before any tenant context exists.
        # PLATFORM_QUERY_OK: this query IS the scoping mechanism the whole product
        # rests on. It runs before app.tenant_id can be set, which is exactly why
        # tenants_membership carries no row-level-security policy.
        # client__isnull=True is a SECURITY control, not a tidy-up. Without it a
        # portal user browsing the FIRM host satisfies this check, receives
        # app.tenant_id, and runs as app_runtime -- for whom no restrictive portal
        # policy applies -- reading the firm's entire client base. That is precisely
        # the hole the portal isolation layer exists to close.
        if not Membership.objects.filter(
            user=user,
            tenant=resolved,
            is_active=True,
            client__isnull=True,
        ).exists():
            msg = "This account has no active membership for this firm."
            raise PermissionDenied(msg)
        return resolved

    def _run_in_tenant_context(
        self,
        request: TenantHttpRequest,
        tenant: Tenant | None,
    ) -> HttpResponseBase:
        request.tenant = tenant
        # token/reset, never a bare set(): gunicorn sync workers reuse one thread
        # across requests, so an unreset ContextVar leaks the previous request's
        # tenant into the next one.
        token = current_tenant_id.set(tenant.id if tenant else None)
        try:
            with transaction.atomic():
                self._apply_guc(tenant)
                response = self.get_response(request)
                self._reject_streaming(response, tenant)
                self._render_inside_transaction(response)
            return response
        finally:
            current_tenant_id.reset(token)

    @staticmethod
    def _apply_guc(tenant: Tenant | None) -> None:
        # A bound parameter, never string interpolation — which is also why SET LOCAL
        # cannot be used here, as it takes no placeholders. The `true` third argument
        # makes the setting transaction-local, so it cannot survive on a pooled
        # connection and leak into the next request.
        value = str(tenant.id) if tenant else NO_TENANT
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, value])

    @staticmethod
    def _reject_streaming(response: HttpResponseBase, tenant: Tenant | None) -> None:
        if tenant is None or not isinstance(response, StreamingHttpResponse):
            return
        # A streaming iterator is consumed by the WSGI server AFTER all middleware
        # returns, by which time this block has exited and the GUC is gone. Every lazy
        # query in the generator would return zero rows, producing a file that
        # downloads successfully and is empty. Materializing it here is not a fix
        # either: on a rolled-back transaction it raises TransactionManagementError
        # and masks the original error. Tenant-scoped views must build their payload
        # in memory and return a plain HttpResponse.
        msg = (
            "A tenant-scoped view returned a streaming response. Its iterator would "
            "be consumed outside the tenant transaction and yield zero rows. Build "
            "the payload in memory and return an HttpResponse instead."
        )
        raise TypeError(msg)

    @staticmethod
    def _render_inside_transaction(response: HttpResponseBase) -> None:
        # Django's BaseHandler normally renders a TemplateResponse before it reaches
        # us. This is the defensive case: a TemplateResponse produced by inner
        # response middleware would otherwise render after this block exits.
        render = getattr(response, "render", None)
        if callable(render) and not getattr(response, "is_rendered", True):
            render()

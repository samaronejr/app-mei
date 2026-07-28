"""Hold the admin's selected firm across the whole request, rendering included.

Wrapping `ModelAdmin.get_queryset()` in `tenant_context` does **not** work, and the
failure is silent. The changelist queryset is lazy and is evaluated while the template
renders, by which time the block has exited and both isolation layers are gone — so
the page renders empty rather than raising. It is the identical failure
`TenantMiddleware` exists to fix, reappearing one layer up.

So the context is established here, around `get_response`, which spans the view *and*
the rendering Django performs inside it.
"""

from collections.abc import Callable
from uuid import UUID

from django.conf import settings
from django.http import Http404, HttpRequest
from django.http.response import HttpResponseBase

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.tenants.admin_tenancy import ADMIN_TENANT_SESSION_KEY, may_operate_as
from apps.tenants.middleware import slug_from_host
from apps.tenants.models import Tenant


class AdminTenantMiddleware:
    """Refuse the admin on a firm's subdomain, and pin the selected firm on it."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the next callable in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Serve an admin request inside the firm it has selected, if any."""
        if not request.path_info.startswith(settings.ADMIN_PATH_PREFIX):
            return self.get_response(request)
        self._refuse_tenant_subdomain(request)
        tenant_id = self._selected_tenant(request)
        if tenant_id is None:
            return self.get_response(request)
        with tenant_context(tenant_id):
            return self.get_response(request)

    @staticmethod
    def _refuse_tenant_subdomain(request: HttpRequest) -> None:
        """Serve the console from the platform hostname only.

        Reachable on a firm's subdomain it would inherit that firm's cookie scope and
        blur the one boundary the whole tenancy model rests on. 404 rather than 403:
        the console's existence is not a fact a firm's subdomain needs to confirm.
        """
        slug = slug_from_host(request.get_host())
        if slug is None:
            return
        # PLATFORM_QUERY_OK: asks only whether the hostname names a firm at all.
        if Tenant.objects.filter(slug=slug).exists():
            raise Http404

    @staticmethod
    def _selected_tenant(request: HttpRequest) -> UUID | None:
        raw = request.session.get(ADMIN_TENANT_SESSION_KEY)
        user = getattr(request, "user", None)
        if raw is None or not isinstance(user, User):
            return None
        tenant_id = UUID(str(raw))
        # Re-checked on every request, not only at selection: revoking a membership
        # must take effect now, not at the operator's next sign-in.
        if not may_operate_as(user, tenant_id):
            del request.session[ADMIN_TENANT_SESSION_KEY]
            return None
        return tenant_id

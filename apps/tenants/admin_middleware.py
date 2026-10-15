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
from typing import cast
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.http.response import HttpResponseBase

from apps.accounts.models import User
from apps.authz.support import (
    SupportContext,
    current_support,
    resolve_support_grant,
)
from apps.core.tenancy import tenant_context
from apps.tenants.admin_tenancy import ADMIN_TENANT_SESSION_KEY, may_operate_as
from apps.tenants.middleware import TenantHttpRequest, slug_from_host
from apps.tenants.models import Membership, Tenant
from apps.tenants.read_only import account_admission


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
            # PLATFORM_QUERY_OK: _selected_tenant just authorized this exact scope.
            tenant = Tenant.objects.get(pk=tenant_id)
            refusal = account_admission(request, tenant, platform=True)
            if refusal is not None:
                return refusal
            # A non-member inside a selected firm is a support session: the live
            # grant is re-resolved inside this request's context (never read from
            # the session that admitted them earlier) and bound as the stash every
            # authz question answers from. Grant-scoped entry is read-only in the
            # console: the model admins' add/change/delete paths bypass can(), so
            # the write refusal lives here rather than in a hundred model forms.
            grant = None
            assert isinstance(request.user, User)  # noqa: S101 — _selected_tenant
            # PLATFORM_QUERY_OK: membership presence on this exact tenant is the
            # check itself — a non-member inside a selected firm is a support
            # session, and only then is the live grant re-resolved.
            if not Membership.objects.filter(
                user=request.user,
                tenant=tenant,
                is_active=True,
                client__isnull=True,
            ).exists():
                grant = resolve_support_grant(request.user.pk, tenant_id)
            # Bind for the outer SupportRequestAuditMiddleware the same way the
            # firm-host middleware does, so admin-console requests under a grant
            # land in the dual audit stream too.
            cast("TenantHttpRequest", request).support_grant = grant
            if grant is not None and request.method not in ("GET", "HEAD", "OPTIONS"):
                raise PermissionDenied
            binding = (
                current_support.set(
                    SupportContext(
                        grant_id=UUID(str(grant.pk)),
                        tenant_id=tenant_id,
                        scope=grant.scope,
                        client_ids=frozenset(grant.client_uuid_set()),
                        expires_at=grant.expires_at,
                    )
                )
                if grant is not None
                else None
            )
            try:
                return self.get_response(request)
            finally:
                if binding is not None:
                    current_support.reset(binding)

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

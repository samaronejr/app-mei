"""Firm-side test routes for the feature-flag gate, plus the real admin router.

The flagged view exists only so `require_feature` has something real to wrap. The
admin mount is the real `admin.site`, needed by the audit-of-tenant-changes test,
which drives `TenantAdmin` through the same POST a platform operator makes.
"""

from uuid import UUID

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.test.utils import CaptureQueriesContext
from django.urls import include, path

from apps.authz.objects import load_visible_client
from apps.authz.services import require_can
from apps.core.flags import require_feature, tenant_has
from apps.tenants.middleware import TenantHttpRequest


@require_feature("cycles_r1")
def flagged(_request: HttpRequest) -> HttpResponse:
    """Stand in for an R1 surface: reachable only while the tenant flag is on."""
    return HttpResponse("reached")


def flagged_queries(request: TenantHttpRequest) -> JsonResponse:
    """Report how many queries one `tenant_has` call costs inside the request.

    The count is captured where the read would happen — inside the view, inside the
    tenant transaction — rather than asserted around the request, which cannot see
    the middleware's own queries from the actual answer.
    """
    tenant = request.tenant
    if tenant is None:
        return JsonResponse({"has": False, "queries": None})
    with CaptureQueriesContext(connection) as seen:
        answered = tenant_has(tenant.pk, "cycles_r1")
    return JsonResponse({"has": answered, "queries": len(seen)})


@require_feature("cycles_r1")
@login_required
@require_can("cycles.operate")
def r1_client_view(request: HttpRequest, client_id: UUID) -> JsonResponse:
    """Stand in for a client-scoped R1 view: capability gate, then the object lens.

    Decorated exactly the way the shipping R1 views will be — flag first so an
    unflagged tenant learns nothing, then `require_can`, then the client loaded
    through `load_visible_client`, which answers 404 for anything outside the
    caller's portfolio rather than confirming the id names a real row.
    """
    client = load_visible_client(request.user, client_id)
    return JsonResponse({"client": str(client.pk)})


@require_feature("cycles_r1")
@login_required
@require_can("actions.approve")
def r1_approve_view(_request: HttpRequest) -> JsonResponse:
    """Stand in for a client-facing approval, gated on the reviewer capability.

    `actions.approve` is NONE for `staff_accountant` by role and raised to FULL by
    an active `UserCapabilityGrant`, which makes this endpoint the instrument the
    grant tests read `resolve_level` through end to end.
    """
    return JsonResponse({"approved": True})


urlpatterns = [
    path("flagged", flagged, name="flagged"),
    path("flagged-queries", flagged_queries, name="flagged-queries"),
    path("r1/clientes/<uuid:client_id>/", r1_client_view, name="r1-client"),
    path("r1/aprovar/", r1_approve_view, name="r1-approve"),
    path("accounts/", include("allauth.urls")),
    # So the team page's reviewer toggle is reachable in this suite the same way it
    # is in the real tree.
    path("", include("apps.accounts.urls")),
    path("admin/", admin.site.urls),
]

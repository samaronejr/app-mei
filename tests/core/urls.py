"""Firm-side test routes for the feature-flag gate, plus the real admin router.

The flagged view exists only so `require_feature` has something real to wrap. The
admin mount is the real `admin.site`, needed by the audit-of-tenant-changes test,
which drives `TenantAdmin` through the same POST a platform operator makes.
"""

from django.contrib import admin
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.test.utils import CaptureQueriesContext
from django.urls import include, path

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


urlpatterns = [
    path("flagged", flagged, name="flagged"),
    path("flagged-queries", flagged_queries, name="flagged-queries"),
    path("accounts/", include("allauth.urls")),
    path("admin/", admin.site.urls),
]

"""An actual write endpoint proves the middleware denies before the view runs."""

from django.http import Http404, HttpRequest, HttpResponse
from django.urls import include, path
from django.views.decorators.http import require_http_methods

from apps.core.tenancy import current_tenant_id
from apps.core.tests.models import ExampleTenantModel


@require_http_methods(["POST", "PUT", "PATCH", "DELETE"])
def write_row(request: HttpRequest) -> HttpResponse:
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise Http404
    ExampleTenantModel.objects.create(tenant_id=tenant_id, name=request.method or "")
    return HttpResponse(status=201)


urlpatterns = [
    path("guard-write/", write_row, name="t22-test-write"),
    path("admin/guard-write/", write_row, name="t22-test-admin-write"),
    path("", include("config.urls")),
]

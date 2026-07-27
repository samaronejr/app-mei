"""Views that exist only to observe what the tenant middleware does around them."""

from collections.abc import Iterator

from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    StreamingHttpResponse,
)
from django.template import engines
from django.template.response import TemplateResponse
from django.urls import path

from apps.core.tests.models import ExampleTenantModel
from apps.tenants.middleware import TenantHttpRequest

ROW_TEMPLATE = "{% for row in rows %}[{{ row.name }}]{% endfor %}"


def guc(_request: HttpRequest) -> HttpResponse:
    """Report the tenant GUC as the database sees it during view execution."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(current_setting('app.tenant_id', true), 'NULL')"
        )
        row = cursor.fetchone()
    return HttpResponse(row[0] if row else "")


def lazy_template(request: HttpRequest) -> TemplateResponse:
    """Return an UNEVALUATED queryset in the context, to be rendered later.

    If rendering happened outside the tenant transaction the queryset would evaluate
    with no `app.tenant_id` and the page would render empty rather than fail.
    """
    template = engines["django"].from_string(ROW_TEMPLATE)
    return TemplateResponse(
        request, template, {"rows": ExampleTenantModel.objects.all()}
    )


def streaming(_request: HttpRequest) -> StreamingHttpResponse:
    """Return a streaming response, which the middleware must refuse."""

    def rows() -> Iterator[bytes]:
        for row in ExampleTenantModel.objects.all():
            yield f"{row.name}\n".encode()

    return StreamingHttpResponse(rows())


def _write_then(request: TenantHttpRequest, name: str) -> None:
    tenant = request.tenant
    assert tenant is not None, "these views are only reachable with a tenant"
    ExampleTenantModel.objects.create(tenant=tenant, name=name)


def write_and_succeed(request: TenantHttpRequest) -> HttpResponse:
    """Write a row and return normally, so the row must survive."""
    _write_then(request, "committed")
    return HttpResponse("ok")


def write_and_raise(request: TenantHttpRequest) -> HttpResponse:
    """Write a row and blow up, so the row must not survive."""
    _write_then(request, "rolled-back")
    msg = "deliberate failure after a partial write"
    raise ValueError(msg)


def write_and_404(request: TenantHttpRequest) -> HttpResponse:
    """Write a row and raise Http404, which converts to 4xx rather than 5xx."""
    _write_then(request, "rolled-back-404")
    raise Http404


def write_and_403(request: TenantHttpRequest) -> HttpResponse:
    """Write a row and raise PermissionDenied, which converts to 4xx."""
    _write_then(request, "rolled-back-403")
    raise PermissionDenied


urlpatterns = [
    path("guc/", guc, name="guc"),
    path("lazy/", lazy_template, name="lazy"),
    path("streaming/", streaming, name="streaming"),
    path("write-ok/", write_and_succeed, name="write-ok"),
    path("write-boom/", write_and_raise, name="write-boom"),
    path("write-404/", write_and_404, name="write-404"),
    path("write-403/", write_and_403, name="write-403"),
]

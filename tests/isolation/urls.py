"""A minimal detail route, so isolation can be asserted at the HTTP layer too."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path

from apps.core.tests.models import ExampleTenantModel


def row_detail(_request: HttpRequest, pk: str) -> HttpResponse:
    """Return one tenant-scoped row, or 404 when it is not this tenant's."""
    row = get_object_or_404(ExampleTenantModel.objects.all(), pk=pk)
    return HttpResponse(row.name)


urlpatterns = [path("rows/<uuid:pk>/", row_detail, name="row-detail")]

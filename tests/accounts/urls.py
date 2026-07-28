"""A tenant view that exists purely to observe what the MFA gate does around it."""

from django.http import HttpRequest, HttpResponse
from django.urls import include, path

from apps.core.views import healthz


def dashboard(_request: HttpRequest) -> HttpResponse:
    """Stand in for every tenant-side screen the MFA gate must protect."""
    return HttpResponse("painel")


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("allauth.urls")),
    path("painel/", dashboard, name="dashboard"),
]

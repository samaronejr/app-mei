"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

from apps.core.dashboard import dashboard_view
from apps.core.search_views import search_view
from apps.core.views import healthz, readyz, versionz
from apps.tenants.admin_views import select_tenant_view

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("versionz", versionz, name="versionz"),
    # The tenant root. On the platform host there is no firm to show a portfolio
    # for, and the view 404s rather than inventing one.
    path("", dashboard_view, name="dashboard"),
    path("busca/", search_view, name="search"),
    path("accounts/", include("allauth.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.clients.urls")),
    path("", include("apps.briefing.urls")),
    path("", include("apps.cycles.urls")),
    path("", include("apps.delivery.urls")),
    path("", include("apps.tenants.urls")),
    path("", include("apps.lgpd.urls")),
    path("", include("apps.obligations.urls")),
    path("", include("apps.core.public_views")),
    # Before admin.site.urls so it is not swallowed by the catch-all admin router.
    path("admin/selecionar-empresa/", select_tenant_view, name="admin-select-tenant"),
    path("admin/", admin.site.urls),
]

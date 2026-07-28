"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

from apps.core.views import healthz
from apps.tenants.admin_views import select_tenant_view

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("allauth.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.clients.urls")),
    path("", include("apps.lgpd.urls")),
    path("", include("apps.obligations.urls")),
    # Before admin.site.urls so it is not swallowed by the catch-all admin router.
    path("admin/selecionar-empresa/", select_tenant_view, name="admin-select-tenant"),
    path("admin/", admin.site.urls),
]

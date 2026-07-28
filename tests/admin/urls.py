"""The real admin router, plus the selector, for admin tests."""

from django.contrib import admin
from django.urls import include, path

from apps.tenants.admin_views import select_tenant_view

urlpatterns = [
    path("accounts/", include("allauth.urls")),
    path("admin/selecionar-empresa/", select_tenant_view, name="admin-select-tenant"),
    path("admin/", admin.site.urls),
]

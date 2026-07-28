"""The real admin router, plus the selector, for admin tests."""

from django.contrib import admin
from django.urls import include, path

from apps.tenants.admin_views import select_tenant_view

urlpatterns = [
    path("accounts/", include("allauth.urls")),
    # The selector renders the site layout, whose footer carries the LGPD rights
    # channel every page owes a data subject. Without this include the page cannot
    # reverse it and the test fails on the layout rather than on the selector.
    path("", include("apps.lgpd.urls")),
    path("admin/selecionar-empresa/", select_tenant_view, name="admin-select-tenant"),
    path("admin/", admin.site.urls),
]

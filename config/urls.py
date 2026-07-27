"""Root URL configuration."""

from django.contrib import admin
from django.urls import path

from apps.core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
]

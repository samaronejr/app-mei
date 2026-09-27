"""Firm-only delivery routes."""

from django.urls import path

from apps.delivery.views import communication

urlpatterns = [path("comunicacao/", communication, name="delivery-queue")]

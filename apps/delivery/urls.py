"""Firm-only delivery routes."""

from django.urls import path

from apps.delivery.automation_views import automations
from apps.delivery.views import communication

urlpatterns = [
    path("comunicacao/", communication, name="delivery-queue"),
    path("gestao/automacoes/", automations, name="automations"),
]

"""Firm-only delivery routes."""

from django.urls import path

from apps.delivery.automation_views import automations
from apps.delivery.notice_views import notice_policy
from apps.delivery.views import communication

urlpatterns = [
    path("gestao/politica-avisos/", notice_policy, name="notice-policy"),
    path("comunicacao/", communication, name="delivery-queue"),
    path("gestao/automacoes/", automations, name="automations"),
]

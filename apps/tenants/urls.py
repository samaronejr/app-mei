"""Firm settings and private account lifecycle routes."""

from django.urls import path

from apps.tenants import account_views, views

urlpatterns = [
    path("configuracoes/", views.settings_view, name="firm-settings"),
    path(
        "configuracoes/preferencias/",
        views.preferences_view,
        name="firm-preferences",
    ),
    path("configuracoes/conta/", account_views.account_view, name="firm-account"),
    path(
        "configuracoes/exportar/",
        account_views.export_request_view,
        name="account-export-request",
    ),
    path(
        "configuracoes/exportacoes/<uuid:export_id>/",
        account_views.export_detail_view,
        name="account-export-detail",
    ),
    path(
        "configuracoes/exportacoes/<uuid:export_id>/arquivo/",
        account_views.export_download_view,
        name="account-export-download",
    ),
    path(
        "configuracoes/retencao/<uuid:client_id>/",
        account_views.retention_view,
        name="firm-retention",
    ),
    path(
        "configuracoes/retencoes/<uuid:hold_id>/liberar/",
        account_views.release_hold_view,
        name="firm-hold-release",
    ),
]

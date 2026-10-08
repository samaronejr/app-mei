"""Firm settings and private account lifecycle routes."""

from django.urls import path

from apps.tenants import account_views, support_views, views

urlpatterns = [
    path("configuracoes/", views.settings_view, name="firm-settings"),
    path(
        "configuracoes/preferencias/",
        views.preferences_view,
        name="firm-preferences",
    ),
    path(
        "configuracoes/suporte/",
        support_views.support_view,
        name="firm-suporte",
    ),
    path(
        "configuracoes/suporte/<uuid:grant_id>/trilha/",
        support_views.support_trail_view,
        name="firm-suporte-trilha",
    ),
    path(
        "configuracoes/suporte/<uuid:grant_id>/aprovar/",
        support_views.approve_support_view,
        name="firm-suporte-approve",
    ),
    path(
        "configuracoes/suporte/<uuid:grant_id>/revogar/",
        support_views.revoke_support_view,
        name="firm-suporte-revoke",
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

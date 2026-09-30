"""Playbook routes — firm-scoped, flag-gated by the views themselves."""

from django.urls import path

from apps.cycles import cycle_views, document_views, views

urlpatterns = [
    path("ciclos/", cycle_views.cycle_list, name="cycle-list"),
    path("ciclos/<uuid:pk>/", cycle_views.cycle_detail, name="cycle-detail"),
    path(
        "ciclos/<uuid:pk>/solicitacoes/",
        cycle_views.cycle_request_action,
        name="cycle-request-action",
    ),
    path("clientes/<uuid:pk>/ciclos/abrir/", cycle_views.cycle_open, name="cycle-open"),
    path(
        "documentos/<uuid:pk>/baixar/",
        document_views.staff_document_download,
        name="staff-document-download",
    ),
    path("gestao/playbooks/", views.playbook_list_view, name="playbook-list"),
    path(
        "gestao/playbooks/<uuid:pk>/",
        views.playbook_detail_view,
        name="playbook-detail",
    ),
    path(
        "gestao/playbooks/<uuid:pk>/v/<int:number>/",
        views.playbook_editor_view,
        name="playbook-editor",
    ),
    path(
        "gestao/playbooks/<uuid:pk>/v/<int:number>/retirar/",
        views.playbook_retire_view,
        name="playbook-retire",
    ),
]

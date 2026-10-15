"""Playbook routes — firm-scoped, flag-gated by the views themselves."""

from django.urls import path

from apps.cycles import (
    approval_views,
    bulk_views,
    cycle_views,
    document_views,
    export_views,
    review_views,
    views,
)
from apps.cycles.publication_views import publication_withdraw

urlpatterns = [
    path(
        "ciclos/<uuid:pk>/evidencias/", export_views.index_page, name="evidence-index"
    ),
    path("ciclos/<uuid:pk>/reabrir/", export_views.reopen_cycle, name="cycle-reopen"),
    path(
        "evidencias/arquivos/<uuid:pk>/",
        export_views.export_detail,
        name="evidence-export-detail",
    ),
    path(
        "evidencias/arquivos/<uuid:pk>/baixar/",
        export_views.export_download,
        name="evidence-export-download",
    ),
    path(
        "publicacoes/<uuid:pk>/retirar/",
        publication_withdraw,
        name="publication-withdraw",
    ),
    path("ciclos/lotes/", bulk_views.bulk_start, name="bulk-start"),
    path("ciclos/lotes/<uuid:pk>/", bulk_views.bulk_detail, name="bulk-detail"),
    path(
        "ciclos/lotes/<uuid:pk>/progresso/",
        bulk_views.bulk_progress,
        name="bulk-progress",
    ),
    path(
        "ciclos/lotes/<uuid:pk>/relatorio.csv",
        bulk_views.bulk_csv,
        name="bulk-report-csv",
    ),
    path("acoes/<uuid:pk>/", approval_views.approval_card, name="approval-card"),
    path(
        "ciclos/<uuid:pk>/propor/",
        approval_views.proposal_create,
        name="proposal-create",
    ),
    path("gestao/aprovacoes/", approval_views.approval_policy, name="approval-policy"),
    path("ciclos/", cycle_views.cycle_list, name="cycle-list"),
    path("ciclos/<uuid:pk>/", cycle_views.cycle_detail, name="cycle-detail"),
    path(
        "ciclos/<uuid:pk>/solicitacoes/",
        cycle_views.cycle_request_action,
        name="cycle-request-action",
    ),
    path(
        "ciclos/pendencias/<uuid:pk>/",
        cycle_views.cycle_exception_action,
        name="cycle-exception-action",
    ),
    path("clientes/<uuid:pk>/ciclos/abrir/", cycle_views.cycle_open, name="cycle-open"),
    path("documentos/", review_views.document_queue, name="document-queue"),
    path(
        "documentos/<uuid:pk>/revisar/",
        review_views.document_review,
        name="document-review",
    ),
    path(
        "documentos/<uuid:pk>/visualizar/",
        review_views.document_preview,
        name="document-preview",
    ),
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

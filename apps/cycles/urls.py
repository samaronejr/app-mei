"""Playbook routes — firm-scoped, flag-gated by the views themselves."""

from django.urls import path

from apps.cycles import views

urlpatterns = [
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

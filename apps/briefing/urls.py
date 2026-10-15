"""Firm-only actions for the root Hoje surface."""

from django.urls import path

from apps.briefing import views

urlpatterns = [
    path("hoje/visualizacoes/", views.save_view_action, name="briefing-save-view"),
    path(
        "hoje/visualizacoes/<uuid:pk>/excluir/",
        views.delete_view_action,
        name="briefing-delete-view",
    ),
    path("hoje/nao-relevante/", views.dismiss_action, name="briefing-dismiss"),
]

"""Client authority routes (AXRM-008), under the client workspace."""

from django.urls import path

from apps.integrations.views import authority_action

urlpatterns = [
    path(
        "clientes/<uuid:pk>/autoridades/<str:service>/<str:action>/",
        authority_action,
        name="client-authority-action",
    ),
]

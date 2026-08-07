"""Client registry routes."""

from django.urls import path

from apps.clients.views import (
    clients_detail_view,
    clients_list_view,
    export_clients_csv,
)

urlpatterns = [
    path("clientes/", clients_list_view, name="clients-list"),
    path("clientes/exportar.csv", export_clients_csv, name="clients-export-csv"),
    # Last, and after the literal export path. Django resolves in declaration order, so
    # a converter pattern written above `exportar.csv` would be consulted first — and
    # while `<uuid:pk>` happens not to match that filename today, ordering the specific
    # path ahead of the general one is what keeps that true if the converter is ever
    # widened. The registry's own path ends in a slash, so this one does too.
    path("clientes/<uuid:pk>/", clients_detail_view, name="client-detail"),
]

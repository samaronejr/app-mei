"""Client registry routes."""

from django.urls import path

from apps.clients.views import clients_list_view, export_clients_csv

urlpatterns = [
    path("clientes/", clients_list_view, name="clients-list"),
    path("clientes/exportar.csv", export_clients_csv, name="clients-export-csv"),
]

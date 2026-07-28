"""Client registry routes."""

from django.urls import path

from apps.clients.views import export_clients_csv

urlpatterns = [
    path("clientes/exportar.csv", export_clients_csv, name="clients-export-csv"),
]

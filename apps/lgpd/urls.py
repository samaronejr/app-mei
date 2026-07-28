"""Routes for the statutory rights channel, mounted at the platform level."""

from django.urls import path

from apps.lgpd.views import data_subject_request_view, request_received_view

urlpatterns = [
    path("lgpd/solicitacao/", data_subject_request_view, name="dsr-submit"),
    path("lgpd/solicitacao/recebida/", request_received_view, name="dsr-received"),
]

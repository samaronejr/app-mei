"""Client authority (AXRM-008) and PAGTOWEB (AXRM-015) routes, under the workspace."""

from django.urls import path

from apps.integrations.serpro.views import (
    consult_federal_payments,
    cycle_federal_payments_section,
    federal_payments_section,
    request_payment_receipt,
)
from apps.integrations.views import authority_action

urlpatterns = [
    path(
        "clientes/<uuid:pk>/autoridades/<str:service>/<str:action>/",
        authority_action,
        name="client-authority-action",
    ),
    path(
        "clientes/<uuid:pk>/receita/pagamentos/competencia/<str:periodo>/",
        federal_payments_section,
        name="client-federal-payments-section",
    ),
    path(
        "ciclos/<uuid:pk>/receita/pagamentos/",
        cycle_federal_payments_section,
        name="cycle-federal-payments-section",
    ),
    path(
        "clientes/<uuid:pk>/receita/pagamentos/consultar/",
        consult_federal_payments,
        name="client-federal-payments-consult",
    ),
    path(
        "clientes/<uuid:pk>/receita/pagamentos/<uuid:payment>/comprovante/",
        request_payment_receipt,
        name="client-federal-payment-receipt",
    ),
]

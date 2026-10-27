"""Staff actions of the "Receita Federal" section: consult payments, request a receipt.

Both only record the intent and enqueue worker tasks after the request commits;
provider I/O never runs inside a web request (A7.4).
"""

from http import HTTPStatus
from typing import Final
from uuid import UUID

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.objects import load_visible_client
from apps.authz.services import require_can
from apps.clients.views import require_tenant
from apps.core.flags import tenant_has
from apps.core.refusal import WayBack, refusal_page
from apps.cycles.services.cycle import tenant_id
from apps.cycles.workspace import parse_period
from apps.integrations.serpro.pagtoweb import PaymentRange
from apps.integrations.serpro.payments import (
    CHECKS,
    FLAG,
    PaymentQueryConflictError,
    queue_payment_consultation,
    queue_payment_receipt,
)

HEADING: Final = "Não foi possível consultar a Receita"


class ConsultForm(forms.Form):
    """The competence the section showed, its newest consultation and a nonce."""

    periodo = forms.RegexField(regex=r"^[0-9]{4}-[0-9]{2}$")
    ultima = forms.UUIDField(required=False)
    nonce = forms.RegexField(regex=r"^[a-f0-9]{32}$")
    ciclo = forms.UUIDField(required=False)


def _back(client_id: UUID, period: str = "", cycle: UUID | None = None) -> str:
    if cycle is not None:
        return reverse("cycle-detail", args=[cycle]) + "#receita-federal"
    query = f"?periodo={period}" if period else ""
    return reverse("client-detail", args=[client_id]) + f"{query}#receita-federal"


def _refuse(
    request: AuthenticatedRequest, client_id: UUID, status: int, reason: str
) -> HttpResponse:
    return refusal_page(
        request,
        status,
        heading=HEADING,
        reason=reason,
        back=WayBack(_back(client_id), "Voltar ao espaço do cliente"),
    )


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can(CHECKS)
def consult_federal_payments(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Queue one payments consultation from the selected competence to today."""
    if not tenant_has(tenant_id(), FLAG):
        raise Http404
    load_visible_client(request.user, pk)
    form = ConsultForm(request.POST)
    if not form.is_valid():
        return _refuse(
            request, pk, HTTPStatus.UNPROCESSABLE_CONTENT, "Dados inválidos."
        )
    data = form.cleaned_data
    try:
        window = PaymentRange(parse_period(data["periodo"]), timezone.localdate())
        queue_payment_consultation(
            request.user,
            pk,
            window,
            expected_latest=data["ultima"],
            nonce=data["nonce"],
        )
    except PaymentQueryConflictError as error:
        return _refuse(request, pk, HTTPStatus.CONFLICT, str(error))
    except (ValidationError, ValueError):
        return _refuse(
            request,
            pk,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            "Escolha uma competência entre os últimos doze meses.",
        )
    except PermissionDenied as error:
        reason = str(error) or "Consulta não permitida para este cliente."
        return _refuse(request, pk, HTTPStatus.UNPROCESSABLE_CONTENT, reason)
    return redirect(_back(pk, data["periodo"], data["ciclo"]))


@require_http_methods(["POST"])
@login_required
@require_tenant
@require_can(CHECKS)
def request_payment_receipt(
    request: AuthenticatedRequest, pk: UUID, payment: UUID
) -> HttpResponse:
    """Queue this payment's COMPARRECADACAO72 receipt (one per payment, ever)."""
    if not tenant_has(tenant_id(), FLAG):
        raise Http404
    load_visible_client(request.user, pk)
    try:
        queue_payment_receipt(request.user, pk, payment)
    except PermissionDenied:
        raise Http404 from None
    return redirect(_back(pk))

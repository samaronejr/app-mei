"""Assisted platform forms call the same audited services as other operators."""

from http import HTTPStatus
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.authz.services import can
from apps.core.tenancy import tenant_context
from apps.tenants.forms import BillingForm, FirmRegistrationForm
from apps.tenants.models import BillingAccount, BillingStatus, Tenant
from apps.tenants.onboarding import (
    BillingChange,
    FirmRegistration,
    create_firm,
    update_billing,
)
from apps.tenants.services import TenantConflictError


def _actor(request: HttpRequest) -> User:
    actor = request.user
    if (
        not isinstance(actor, User)
        or not actor.is_active
        or not actor.is_staff
        or not can(actor, "tenants.create")
        or not can(actor, "billing.manage")
    ):
        raise PermissionDenied
    return actor


@require_http_methods(["GET", "POST"])
def new_firm_view(request: HttpRequest) -> HttpResponse:
    """Queue the owner invitation as part of a real, audited firm creation."""
    actor = _actor(request)
    form = FirmRegistrationForm(request.POST or None)
    status = HTTPStatus.OK
    if request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if form.is_valid():
            try:
                tenant = create_firm(actor, FirmRegistration(**form.cleaned_data))
            except ValidationError as error:
                form.add_error(None, " ".join(error.messages))
            else:
                messages.success(
                    request, "Firma criada. O convite do responsável está na fila."
                )
                return HttpResponseRedirect(
                    reverse("admin:tenants_tenant_billing", args=[tenant.pk])
                )
    return render(
        request,
        "tenants/operator_form.html",
        {"form": form, "page_title": "Nova firma", "submit_label": "Criar firma"},
        status=status,
    )


@require_http_methods(["GET", "POST"])
def billing_view(request: HttpRequest, tenant_id: UUID) -> HttpResponse:
    """Record or correct a manual subscription, including restoring a suspended one."""
    actor = _actor(request)
    # PLATFORM_QUERY_OK: platform billing authority was checked before this lookup.
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        raise Http404
    with tenant_context(tenant_id):
        account = BillingAccount.objects.filter(tenant=tenant).first()
        form = BillingForm(
            request.POST or None,
            initial={
                "version": account.version if account else 0,
                "plan_band": account.plan_band if account else "",
                "status": account.status if account else BillingStatus.ACTIVE,
                "next_invoice_on": (
                    account.next_invoice_on.isoformat()
                    if account and account.next_invoice_on is not None
                    else ""
                ),
                "active_client_limit": tenant.active_client_limit,
            },
        )
    status = HTTPStatus.OK
    if request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if form.is_valid():
            data = form.cleaned_data
            try:
                update_billing(
                    actor,
                    tenant_id,
                    BillingChange(
                        data["plan_band"],
                        data["status"],
                        data["next_invoice_on"],
                        data["active_client_limit"],
                        data["version"],
                    ),
                )
            except TenantConflictError as error:
                form.add_error(None, str(error))
                status = HTTPStatus.CONFLICT
            except ValidationError as error:
                form.add_error(None, " ".join(error.messages))
            else:
                messages.success(request, "Registro manual de cobrança atualizado.")
                return HttpResponseRedirect(request.path)
    return render(
        request,
        "tenants/operator_form.html",
        {
            "form": form,
            "page_title": f"Cobrança manual: {tenant.name}",
            "submit_label": "Salvar registro manual",
        },
        status=status,
    )

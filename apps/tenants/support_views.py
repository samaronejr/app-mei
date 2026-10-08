"""The two surfaces a support grant lives on.

`support_request_view` is the platform side: a staff account states a purpose and
opens a pending request against one firm. It renders through the console's own
admin path so `staff_member_required` — the same check the rest of the console
passes — is what admits it.

`support_view`, `approve_support_view` and `revoke_support_view` are the owner
side: the firm's own page inside `/configuracoes/`, behind the account-owner
gate `account_view` already uses. The owner sees every request with its purpose,
scope and client list, approves with a duration (1-72h) and revokes with one
POST. A support session itself can never reach these: `require_account_owner`
checks membership, which a platform actor holds none of, and the explicit
`in_support_mode` guard keeps the answer 403 even if that invariant ever moved.
"""

from http import HTTPStatus
from typing import Any
from uuid import UUID

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
)
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import User
from apps.accounts.views import AuthenticatedRequest
from apps.authz.firm_account import ACCOUNT_CAPABILITY, require_account_owner
from apps.authz.services import require_can
from apps.authz.support import in_support_mode
from apps.clients.models import ClientCompany
from apps.clients.views import require_tenant
from apps.core.flags import require_feature
from apps.tenants.models import Tenant
from apps.tenants.support import (
    MAX_HOURS,
    SupportApproval,
    approve_support_grant,
    decided_support_grants,
    pending_support_grants,
    request_support_grant,
    revoke_support_grant,
)
from apps.tenants.support_models import SupportScope
from apps.tenants.views import FLAG, resolved_firm


class SupportRequestForm(forms.Form):
    """The platform-side POST contract: a firm and a stated purpose."""

    tenant = forms.ModelChoiceField(
        label=gettext_lazy("Firma"),
        # PLATFORM_QUERY_OK: the picker lists active firms for a platform
        # operator; an operator holds no firm seat to scope the list through.
        queryset=Tenant.objects.filter(is_active=True).order_by("slug"),
    )
    purpose = forms.CharField(
        label=gettext_lazy("Finalidade do acesso"),
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class SupportApprovalForm(forms.Form):
    """The owner's decision: scope, clients and a duration inside 72 hours."""

    scope = forms.ChoiceField(
        label=gettext_lazy("Tipo de acesso"),
        choices=SupportScope.choices,
    )
    hours = forms.IntegerField(
        label=gettext_lazy("Duração (horas)"),
        min_value=1,
        max_value=MAX_HOURS,
        initial=4,
    )
    client_ids = forms.ModelMultipleChoiceField[ClientCompany](
        label=gettext_lazy("Clientes visíveis (vazio = somente dados do escritório)"),
        queryset=ClientCompany.objects.none(),
        required=False,
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def support_request_view(request: HttpRequest) -> HttpResponse:
    """Open a pending grant request for one firm, with a stated purpose."""
    assert isinstance(request.user, User)  # noqa: S101 — staff_member_required
    form = SupportRequestForm(request.POST or None)
    status = HTTPStatus.OK
    if request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if form.is_valid():
            tenant = form.cleaned_data["tenant"]
            assert isinstance(tenant, Tenant)  # noqa: S101 — ModelChoiceField
            try:
                request_support_grant(
                    request.user,
                    tenant.pk,
                    purpose=str(form.cleaned_data["purpose"]),
                )
            except (Http404, ValidationError) as error:
                form.add_error(None, str(error))
            else:
                messages.success(
                    request,
                    "Solicitação enviada ao responsável pela firma.",
                )
                return HttpResponseRedirect(request.path)
    return render(
        request,
        "tenants/operator_form.html",
        {
            "form": form,
            "page_title": "Solicitar acesso de suporte",
            "submit_label": "Enviar solicitação",
        },
        status=status,
    )


def _client_bound_field(tenant: Tenant) -> forms.BoundField:
    """Build a bound checkbox list of exactly this firm's clients for the template."""
    form = SupportApprovalForm()
    field: Any = form.fields["client_ids"]
    field.queryset = ClientCompany.objects.filter(tenant=tenant).order_by("legal_name")
    return form["client_ids"]


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def support_view(request: AuthenticatedRequest) -> HttpResponse:
    """List pending and decided grants for the firm the request resolves to."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    return render(
        request,
        "tenants/support.html",
        {
            "firm": tenant,
            "pending_requests": pending_support_grants(tenant.pk),
            "decided": decided_support_grants(tenant.pk),
            "client_field": _client_bound_field(tenant),
            "scope_choices": SupportScope.choices,
            "max_hours": MAX_HOURS,
        },
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def approve_support_view(request: AuthenticatedRequest, grant_id: UUID) -> HttpResponse:
    """Approve one pending grant with the owner's scope, clients and duration."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    if in_support_mode():
        raise PermissionDenied
    form = SupportApprovalForm(request.POST)
    client_field: Any = form.fields["client_ids"]
    client_field.queryset = ClientCompany.objects.filter(tenant=tenant)
    if not form.is_valid():
        messages.error(request, "Revise os dados da concessão.")
        return HttpResponseRedirect(reverse("firm-suporte"))
    decision = SupportApproval(
        scope=str(form.cleaned_data["scope"]),
        client_ids=tuple(client.pk for client in form.cleaned_data["client_ids"]),
        hours=int(form.cleaned_data["hours"]),
    )
    try:
        approve_support_grant(request.user, tenant.pk, grant_id, decision)
    except (Http404, ValidationError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Acesso de suporte concedido.")
    return HttpResponseRedirect(reverse("firm-suporte"))


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ACCOUNT_CAPABILITY)
def revoke_support_view(request: AuthenticatedRequest, grant_id: UUID) -> HttpResponse:
    """Revoke one grant — pending or live — and take effect on the next request."""
    tenant = resolved_firm(request)
    require_account_owner(request.user, tenant.pk)
    if in_support_mode():
        raise PermissionDenied
    try:
        revoke_support_grant(request.user, tenant.pk, grant_id)
    except (Http404, ValidationError) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Acesso de suporte encerrado.")
    return HttpResponseRedirect(reverse("firm-suporte"))


__all__ = [
    "approve_support_view",
    "revoke_support_view",
    "support_request_view",
    "support_view",
]

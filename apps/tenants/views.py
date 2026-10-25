"""Firm profile and personal notifications within the existing product shell."""

from http import HTTPStatus

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.authz.firm_account import can_manage_account
from apps.authz.services import can, require_can
from apps.clients.views import require_tenant
from apps.core.flags import require_feature
from apps.integrations.panel import fiscal_connections_panel
from apps.tenants.account_state import read_only_reason
from apps.tenants.forms import NotificationForm, ProfileForm
from apps.tenants.models import NotificationPreference, Tenant
from apps.tenants.services import (
    FirmProfile,
    TenantConflictError,
    save_notification_preference,
    save_profile,
)

FLAG = "firm_settings_r1"


def resolved_firm(request: AuthenticatedRequest) -> Tenant:
    """Use only the firm granted by middleware, not a submitted account id."""
    tenant = getattr(request, "tenant", None)
    if not isinstance(tenant, Tenant):
        raise Http404
    return tenant


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can("clients.view_assigned")
def settings_view(request: AuthenticatedRequest) -> HttpResponse:
    """Show profile editing only to billing managers; every member gets preferences."""
    tenant = resolved_firm(request)
    may_edit = can(request.user, "billing.manage", tenant_id=tenant.pk)
    form = ProfileForm(
        request.POST or None,
        request.FILES or None,
        initial={
            "version": tenant.settings_version,
            "name": tenant.name,
            "brand_name": tenant.brand_name,
            "brand_color": tenant.brand_color,
        },
    )
    status = HTTPStatus.OK
    if request.method == "POST":
        if not may_edit:
            raise PermissionDenied
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if form.is_valid():
            data = form.cleaned_data
            upload = data["logo"]
            try:
                save_profile(
                    request.user,
                    tenant.pk,
                    FirmProfile(
                        data["name"],
                        data["brand_name"],
                        data["brand_color"],
                        upload.read() if upload else None,
                        data["remove_logo"],
                    ),
                    expected_version=data["version"],
                )
            except TenantConflictError as error:
                form.add_error(None, str(error))
                status = HTTPStatus.CONFLICT
            except ValidationError as error:
                form.add_error(None, " ".join(error.messages))
            else:
                messages.success(request, "Configurações salvas.")
                return HttpResponseRedirect(request.path)
    return render(
        request,
        "tenants/settings.html",
        {
            "form": form,
            "firm": tenant,
            "may_edit": may_edit,
            "may_account": can_manage_account(request.user, tenant.pk),
            "may_team": can(request.user, "users.create", tenant_id=tenant.pk),
            "read_only": read_only_reason(tenant),
            "fiscal_connections": fiscal_connections_panel(request.user, tenant.pk),
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can("clients.view_assigned")
def preferences_view(request: AuthenticatedRequest) -> HttpResponse:
    """Opt in or out without sending mail inside the request transaction."""
    tenant = resolved_firm(request)
    preference = NotificationPreference.objects.filter(user=request.user).first()
    form = NotificationForm(
        request.POST or None,
        initial={
            "version": preference.version if preference else 0,
            "daily_summary": preference.daily_summary if preference else False,
        },
    )
    status = HTTPStatus.OK
    if request.method == "POST":
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if form.is_valid():
            try:
                save_notification_preference(
                    request.user,
                    tenant.pk,
                    enabled=form.cleaned_data["daily_summary"],
                    expected_version=form.cleaned_data["version"],
                )
            except TenantConflictError as error:
                form.add_error(None, str(error))
                status = HTTPStatus.CONFLICT
            else:
                messages.success(request, "Preferências salvas.")
                return HttpResponseRedirect(request.path)
    return render(
        request,
        "tenants/preferences.html",
        {"form": form, "read_only": read_only_reason(tenant)},
        status=status,
    )

"""The tenant selector itself."""

from http import HTTPStatus
from uuid import UUID

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.tenants.admin_tenancy import (
    ADMIN_TENANT_SESSION_KEY,
    AdminTenantError,
    authorize,
    record_selection,
    selectable_tenants,
)

TEMPLATE = "admin/select_tenant.html"


@staff_member_required
@require_http_methods(["GET", "POST"])
def select_tenant_view(request: HttpRequest) -> HttpResponse:
    """Choose the firm this admin session operates as, and record the choice."""
    user = request.user
    assert isinstance(user, User)  # noqa: S101 — staff_member_required guarantees it

    if request.method == "GET":
        return render(request, TEMPLATE, _context(user))

    try:
        tenant, break_glass = authorize(
            user=user,
            tenant_id=UUID(str(request.POST.get("tenant", ""))),
            reason=request.POST.get("reason", ""),
        )
    except (AdminTenantError, ValueError) as error:
        return render(
            request,
            TEMPLATE,
            _context(user, error=str(error) or _("Empresa inválida.")),
            status=HTTPStatus.FORBIDDEN,
        )

    record_selection(
        user=user,
        tenant=tenant,
        break_glass=break_glass,
        reason=request.POST.get("reason", ""),
    )
    request.session[ADMIN_TENANT_SESSION_KEY] = str(tenant.pk)
    return HttpResponseRedirect(request.POST.get("next") or "/admin/")


def _context(user: User, error: str = "") -> dict[str, object]:
    return {
        "tenants": selectable_tenants(user),
        "may_break_glass": user.is_superuser,
        "error": error,
    }

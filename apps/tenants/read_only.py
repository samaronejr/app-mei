"""One admission policy shared by firm, portal and selected-tenant admin middleware."""

from typing import Final

from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from django.urls import Resolver404, resolve

from apps.tenants.account_state import (
    READ_ONLY_MESSAGE,
    export_available,
    read_only_reason,
)
from apps.tenants.models import Tenant

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
EXPORT_READS: Final = frozenset(
    {"account-export-download", "evidence-export-download", "clients-export-csv"}
)
SESSION_POSTS: Final = frozenset(
    {
        "account_logout",
        "mfa_authenticate",
        "portal-company-select",
        "admin-select-tenant",
    }
)
PLATFORM_ACCOUNT_COMMANDS: Final = frozenset(
    {"admin:tenants_tenant_new_firm", "admin:tenants_tenant_billing"}
)
EXPORT_ENDED: Final = "O prazo de exportação desta conta encerrou."
EXPORT_ENDED_TEMPLATE: Final = "refusal/export_ended.html"


def account_admission(
    request: HttpRequest, tenant: Tenant | None, *, platform: bool = False
) -> HttpResponse | None:
    """Refuse before the view, including HTMX and views that enqueue Celery work."""
    if tenant is None:
        return None
    method = request.method or ""
    if method in SAFE_METHODS and tenant.cancellation_ends_on is None:
        return None
    try:
        match = resolve(request.path_info, urlconf=getattr(request, "urlconf", None))
        name, view_name = match.url_name, match.view_name
    except Resolver404:
        name, view_name = None, ""
    exporting = (method in {"GET", "HEAD"} and name in EXPORT_READS) or (
        method == "POST"
        and (
            name == "account-export-request"
            or (name == "evidence-index" and request.POST.get("action") == "export")
        )
    )
    if exporting and not export_available(tenant):
        # AXR1-054: a page rather than bare text. Rendered without the request, so
        # no context processor runs and the page reads nothing on any of the three
        # hosts that share this policy.
        return HttpResponse(
            render_to_string(EXPORT_ENDED_TEMPLATE, {"reason": EXPORT_ENDED}),
            status=410,
        )
    if (
        exporting
        or method in SAFE_METHODS
        or (method == "POST" and name in SESSION_POSTS)
        or (platform and view_name in PLATFORM_ACCOUNT_COMMANDS)
    ):
        return None
    if read_only_reason(tenant):
        response = HttpResponse(
            READ_ONLY_MESSAGE, status=403, content_type="text/plain; charset=utf-8"
        )
        response["X-Tenant-Read-Only"] = "1"
        return response
    return None

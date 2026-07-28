"""Views that audit something and then fail, so the transaction boundary is visible."""

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.urls import path

from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, Origin, record_event, record_platform_event
from apps.tenants.middleware import TenantHttpRequest

DENIED_SUBJECT = "quem@tentou.example"


def audit_then_deny(request: TenantHttpRequest) -> HttpResponse:
    """Record both kinds of event, then raise — the shape a denied action has."""
    tenant = request.tenant
    assert tenant is not None, "this view is only reachable with a resolved tenant"
    record_event(
        action=AuditAction.EXPORT,
        tenant_id=tenant.id,
        obj=ObjectRef(type="ClientCompany", id="1"),
    )
    record_platform_event(
        action=AuditAction.DENIED,
        origin=Origin(subject=DENIED_SUBJECT),
        tenant_id=tenant.id,
    )
    raise PermissionDenied


def audit_then_succeed(request: TenantHttpRequest) -> HttpResponse:
    """Record both kinds of event and return normally, so both must survive."""
    tenant = request.tenant
    assert tenant is not None, "this view is only reachable with a resolved tenant"
    record_event(
        action=AuditAction.EXPORT,
        tenant_id=tenant.id,
        obj=ObjectRef(type="ClientCompany", id="1"),
    )
    record_platform_event(
        action=AuditAction.DENIED,
        origin=Origin(subject=DENIED_SUBJECT),
        tenant_id=tenant.id,
    )
    return HttpResponse("ok")


def ping(_request: HttpRequest) -> HttpResponse:
    """A view that needs no tenant, so anonymous requests can reach it."""
    return HttpResponse("pong")


urlpatterns = [
    path("ping/", ping, name="ping"),
    path("audit-deny/", audit_then_deny, name="audit-deny"),
    path("audit-ok/", audit_then_succeed, name="audit-ok"),
]

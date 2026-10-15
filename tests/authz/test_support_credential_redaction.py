"""Support audit sinks and historical owner trails must not expose credentials."""

from http import HTTPStatus

import pytest
from django.urls import reverse

from apps.audit.models import AccessLog, AuditAction, Event, PlatformEvent
from apps.core.tenancy import tenant_context
from tests.authz.test_support_grant import (
    _firm_host,
    _grant_for,
    _session_for,
    _staff,
)
from tests.tenants.test_cancellation_retention import enabled_firm

pytestmark = pytest.mark.django_db(transaction=True)


def test_support_audit_redacts_registered_path_credentials() -> None:
    """A resolved invitation 404 remains audited without its credential or query."""
    firm = enabled_firm("support-path-redaction")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    needle = "SUPPORT_PATH_CREDENTIAL_CANARY"
    path = reverse("invite-accept", args=[needle])
    session = _session_for(staff, _firm_host(firm))

    response = session.get(f"{path}?private=QUERY_CANARY")

    assert response.status_code == HTTPStatus.NOT_FOUND
    platform = PlatformEvent.objects.get(action=AuditAction.SUPPORT_REQUEST)
    with tenant_context(firm.tenant.pk):
        tenant_event = Event.objects.get(action=AuditAction.SUPPORT_REQUEST)
    expected = "/convites/aceitar/<redacted>/"
    assert AccessLog.objects.get(path__startswith="/convites/aceitar/").path == (
        f"{expected}?private=QUERY_CANARY"
    )
    assert platform.metadata["path"] == expected
    assert tenant_event.metadata["path"] == expected
    owner = _session_for(firm.owner, _firm_host(firm))
    trail = owner.get(f"/configuracoes/suporte/{grant.pk}/trilha/")
    assert trail.status_code == HTTPStatus.OK
    assert needle not in trail.content.decode()
    assert "QUERY_CANARY" not in trail.content.decode()


def test_owner_trail_redacts_preexisting_path_credentials() -> None:
    """Old append-only audit rows are sanitized when the owner reads them."""
    firm = enabled_firm("support-old-path-redaction")
    staff = _staff()
    grant = _grant_for(firm.tenant, firm.owner, staff)
    needle = "HISTORICAL_PATH_CREDENTIAL_CANARY"
    with tenant_context(firm.tenant.pk):
        Event.objects.create(
            tenant=firm.tenant,
            actor=staff,
            action=AuditAction.SUPPORT_REQUEST,
            object_type="SupportGrant",
            object_id=str(grant.pk),
            metadata={
                "method": "GET",
                "path": reverse("invite-accept", args=[needle]),
                "status_code": HTTPStatus.NOT_FOUND,
            },
        )
    owner = _session_for(firm.owner, _firm_host(firm))

    trail = owner.get(f"/configuracoes/suporte/{grant.pk}/trilha/")

    assert trail.status_code == HTTPStatus.OK
    assert needle not in trail.content.decode()
    assert trail.context["rows"][0]["path"] == "/convites/aceitar/<redacted>/"

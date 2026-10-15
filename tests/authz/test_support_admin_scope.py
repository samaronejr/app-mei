"""Support grants constrain the rendered admin audit surfaces."""

from http import HTTPStatus

import pytest

from apps.audit.models import AccessLog, AuditAction, Event, PlatformEvent
from apps.core.tenancy import tenant_context
from tests.authz.test_support_grant import (
    _grant_for,
    _session_for,
    _staff,
)
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import add_client, client_base

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("listed", [False, True])
def test_admin_audit_respects_support_client_scope(listed: bool) -> None:
    # Given two clients' audit rows and a metadata-only or client-limited grant.
    firm = enabled_firm("support-admin-audit")
    add_client(firm, legal_name="Excluded client", base=client_base(2, "audit-scope"))
    staff = _staff()
    staff.is_superuser = True
    staff.save(update_fields=["is_superuser"])
    grant = _grant_for(
        firm.tenant,
        firm.owner,
        staff,
        client_ids=(firm.clients[0].pk,) if listed else (),
    )
    with tenant_context(firm.tenant.pk):
        allowed = Event.objects.create(
            tenant=firm.tenant,
            action=AuditAction.CLIENT_UPDATED,
            object_type="ClientCompany",
            object_id=str(firm.clients[0].pk),
        )
        excluded = Event.objects.create(
            tenant=firm.tenant,
            action=AuditAction.CLIENT_UPDATED,
            object_type="ClientCompany",
            object_id=str(firm.clients[1].pk),
        )
        metadata_row = Event.objects.create(
            tenant=firm.tenant,
            action=AuditAction.EXPORT,
            object_type="Document",
            object_id="excluded-document",
            metadata={"client_id": str(firm.clients[1].pk)},
        )
    session = _session_for(staff, "localhost")
    selected = session.post(
        "/admin/selecionar-empresa/",
        {"tenant": str(firm.tenant.pk), "reason": str(grant.pk)},
    )
    assert selected.status_code == HTTPStatus.FOUND

    # When the selected firm's audit changelist renders.
    page = session.get("/admin/audit/event/")

    # Then only approved client references can reach either list or detail.
    assert page.status_code == HTTPStatus.OK
    html = page.content.decode()
    assert (str(allowed.pk) in html) is listed
    assert str(excluded.pk) not in html
    assert str(metadata_row.pk) not in html
    detail = session.get(f"/admin/audit/event/{excluded.pk}/change/")
    assert detail.status_code in (
        HTTPStatus.FOUND,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
    )


@pytest.mark.parametrize("model", [PlatformEvent, AccessLog])
def test_unclassified_platform_audit_is_hidden_during_support(
    model: type[PlatformEvent] | type[AccessLog],
) -> None:
    # Given a platform stream without an enforceable per-client partition.
    firm = enabled_firm("support-admin-stream")
    staff = _staff()
    staff.is_superuser = True
    staff.save(update_fields=["is_superuser"])
    grant = _grant_for(firm.tenant, firm.owner, staff)
    row: PlatformEvent | AccessLog
    if model is PlatformEvent:
        row = PlatformEvent.objects.create(
            tenant=firm.tenant,
            action=AuditAction.EXPORT,
            metadata={"client_id": str(firm.clients[0].pk)},
        )
    else:
        row = AccessLog.objects.create(
            tenant=firm.tenant,
            method="GET",
            path=f"/clientes/{firm.clients[0].pk}/",
            status_code=200,
        )
    session = _session_for(staff, "localhost")
    session.post(
        "/admin/selecionar-empresa/",
        {"tenant": str(firm.tenant.pk), "reason": str(grant.pk)},
    )

    # When the raw platform audit changelist renders under the grant.
    page = session.get(f"/admin/audit/{model._meta.model_name}/")

    # Then the grant does not buy access to the raw stream.
    assert page.status_code == HTTPStatus.OK
    assert str(row.pk) not in page.content.decode()

"""Suspension denies every unsafe method before any normal or queued effect."""

from http import HTTPStatus
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.clients.importing import plan_import
from apps.clients.models import ImportBatch, ImportStatus
from apps.clients.tasks import commit_import_task
from apps.core.tenancy import tenant_context
from apps.core.tests.models import ExampleTenantModel
from apps.cycles.models import EvidenceLink
from apps.obligations.models import Document
from apps.tenants.models import BillingAccount
from tests.clients.test_import_wizard import csv_upload
from tests.portal.request_fixtures import portal_setup, upload
from tests.ui.factories import Firm, client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _routes(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.tenants.read_only_urls"
    settings.ALLOWED_HOSTS = ["localhost", ".localhost", "testserver"]


def firm_session(slug: str, status: str) -> tuple[Firm, Client]:
    firm = make_firm(slug, client_count=1)
    firm.tenant.features = ["cycles_r1", "import_r1"]
    firm.tenant.save(update_fields=["features"])
    http = firm.as_owner()
    with tenant_context(firm.tenant.pk):
        BillingAccount.objects.create(tenant=firm.tenant, status=status)
    return firm, http


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("status", ["active", "past_due", "suspended"])
def test_all_unsafe_methods_are_blocked_before_the_view(
    method: str, htmx: bool, status: str
) -> None:
    firm, http = firm_session("suspension-methods", status)
    response = http.generic(
        method, "/guard-write/", headers={"HX-Request": "true"} if htmx else {}
    )
    blocked = status == "suspended"
    assert response.status_code == (
        HTTPStatus.FORBIDDEN if blocked else HTTPStatus.CREATED
    )
    with tenant_context(firm.tenant.pk):
        assert ExampleTenantModel.objects.count() == (0 if blocked else 1)
    if blocked:
        assert response["X-Tenant-Read-Only"] == "1"
        assert "somente leitura" in response.content.decode().lower()


@pytest.mark.parametrize("status", ["past_due", "suspended"])
def test_reading_and_csv_export_remain_available(status: str) -> None:
    firm, http = firm_session("suspension-read-export", status)
    listing = http.get(reverse("clients-list"))
    exported = http.get(reverse("clients-export-csv"))
    assert listing.status_code == HTTPStatus.OK
    assert exported.status_code == HTTPStatus.OK
    assert firm.clients[0].cnpj in exported.content.decode()


@pytest.mark.parametrize("status", ["active", "suspended"])
def test_a_celery_triggering_view_cannot_queue_when_suspended(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    firm, http = firm_session("suspension-enqueue", "active")
    with tenant_context(firm.tenant.pk):
        plan = plan_import(
            firm.owner,
            csv_upload(
                [(make_cnpj(client_base(2, "suspension-enqueue")), "Novo", "SP")]
            ),
            None,
        )
        BillingAccount.objects.filter(tenant=firm.tenant).update(status=status)
    queued: list[tuple[str, str, str, int]] = []
    monkeypatch.setattr(commit_import_task, "delay", lambda *args: queued.append(args))
    response = http.post(
        reverse("client-import-commit", args=[plan.batch_id]),
        {"version": plan.version},
        headers={"HX-Request": "true"},
    )
    blocked = status == "suspended"
    assert response.status_code == (
        HTTPStatus.FORBIDDEN if blocked else HTTPStatus.FOUND
    )
    assert len(queued) == (0 if blocked else 1)
    with tenant_context(firm.tenant.pk):
        batch = ImportBatch.objects.get(pk=plan.batch_id)
        assert batch.status == (
            ImportStatus.PLANNED if blocked else ImportStatus.QUEUED
        )
        assert batch.version == (1 if blocked else 2)


@pytest.mark.parametrize("status", ["active", "suspended"])
def test_selected_tenant_admin_writes_share_the_read_only_gate(status: str) -> None:
    firm = make_firm("suspension-admin")
    firm.owner.is_staff = True
    firm.owner.save(update_fields=["is_staff"])
    http = Client(SERVER_NAME="localhost")
    http.force_login(firm.owner)
    selected = http.post(
        "/admin/selecionar-empresa/",
        {"tenant": str(firm.tenant.pk), "next": "/admin/guard-write/"},
    )
    assert selected.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.pk):
        BillingAccount.objects.create(tenant=firm.tenant, status=status)
    response = http.post("/admin/guard-write/")
    assert response.status_code == (
        HTTPStatus.FORBIDDEN if status == "suspended" else HTTPStatus.CREATED
    )
    with tenant_context(firm.tenant.pk):
        assert ExampleTenantModel.objects.count() == (0 if status == "suspended" else 1)


def test_logout_is_available_to_a_suspended_tenant() -> None:
    _firm, http = firm_session("suspension-logout", "suspended")
    response = http.post(reverse("account_logout"))
    assert response.status_code == HTTPStatus.FOUND
    assert "_auth_user_id" not in http.session


@pytest.mark.parametrize("status", ["active", "suspended"])
@pytest.mark.parametrize("htmx", [False, True])
def test_portal_upload_is_checked_before_switching_database_role(
    status: str, htmx: bool, settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = portal_setup("suspension-portal")
    with tenant_context(setup.graph.tenant.pk):
        BillingAccount.objects.create(tenant=setup.graph.tenant, status=status)
    response = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("recibo.pdf", b"%PDF-1.7\nsynthetic receipt\n%%EOF")},
        headers={"HX-Request": "true"} if htmx else {},
    )
    blocked = status == "suspended"
    assert response.status_code == (
        HTTPStatus.FORBIDDEN if blocked else HTTPStatus.OK if htmx else HTTPStatus.FOUND
    )
    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.count() == (0 if blocked else 1)
        assert EvidenceLink.objects.count() == (0 if blocked else 1)

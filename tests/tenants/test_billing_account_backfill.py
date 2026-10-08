"""Every firm holds its one manual billing account, including firms older than it.

`create_firm` opens the account in the same transaction as the firm (AXR1-022), but
`tenants.0011` created the table without a row for the firms that already existed,
and the legacy admin "add" form saved a firm without one. The account page reads
that row, so such a firm got a 500 the moment `firm_settings_r1` was enabled.

The backfill is measured through the migration executor, as
`tests/clients/test_backfill_v1.py` explains: `RESET ROLE` returns the connection to
`app_test` (BYPASSRLS, like `app_migrator` in production) and a fresh executor
replays exactly the migration under test after `record_unapplied`.
"""

from http import HTTPStatus

import pytest
from django.apps.registry import Apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.tenants.models import BillingAccount, BillingStatus, Tenant
from tests.support import enrol_totp
from tests.tenants.test_cancellation_retention import enabled_firm

pytestmark = pytest.mark.django_db(transaction=True)

APP = "tenants"
BACKFILL = "0013_backfill_billing_accounts"


def _replay_backfill() -> Apps:
    """Run the backfill again against the fully migrated schema."""
    executor = MigrationExecutor(connection)
    executor.recorder.record_unapplied(APP, BACKFILL)
    fresh = MigrationExecutor(connection)
    fresh.migrate([(APP, BACKFILL)])
    return fresh.loader.project_state((APP, BACKFILL)).apps


def _as_migrator() -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


def _as_runtime() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE app_runtime")


def _accounts(apps: Apps, tenant: Tenant) -> list[tuple[str, str, str, int]]:
    model = apps.get_model("tenants", "BillingAccount")
    return [
        (row.provider, row.status, row.plan_band, row.version)
        for row in model.objects.filter(tenant_id=tenant.pk)
    ]


def test_backfill_opens_one_account_per_firm_and_keeps_recorded_ones() -> None:
    legacy = Tenant.objects.create(name="Firma antiga", slug="firma-antiga")
    recorded = Tenant.objects.create(name="Firma com conta", slug="firma-com-conta")
    with tenant_context(recorded.pk):
        BillingAccount.objects.create(
            tenant=recorded, plan_band="B", status=BillingStatus.PAST_DUE, version=3
        )
    _as_migrator()
    try:
        after = _replay_backfill()
        # The account create_firm would have opened: manual, active, no band.
        assert _accounts(after, legacy) == [("manual", "active", "", 1)]
        # An operator's record is never overwritten by the backfill.
        assert _accounts(after, recorded) == [("manual", "past_due", "B", 3)]
        # A rerun converges instead of tripping the one-account constraint.
        again = _replay_backfill()
        assert _accounts(again, legacy) == [("manual", "active", "", 1)]
    finally:
        _as_runtime()


def test_account_page_renders_for_a_firm_created_before_billing_accounts() -> None:
    firm = enabled_firm("conta-legada")
    with tenant_context(firm.tenant.pk):
        assert not BillingAccount.objects.exists()
    _as_migrator()
    try:
        _replay_backfill()
    finally:
        _as_runtime()
    response = firm.as_owner().get(reverse("firm-account"))
    assert response.status_code == HTTPStatus.OK
    assert "Ativa (registro manual)" in response.content.decode()


def test_account_page_reports_a_pending_record_while_no_account_exists() -> None:
    firm = enabled_firm("conta-sem-registro")
    with tenant_context(firm.tenant.pk):
        assert not BillingAccount.objects.exists()
    response = firm.as_owner().get(reverse("firm-account"))
    assert response.status_code == HTTPStatus.OK
    assert "Registro pendente" in response.content.decode()


def test_admin_added_firm_opens_its_billing_account() -> None:
    operator = User.objects.create_superuser(email="root@billing.example")
    enrol_totp(operator)
    http = Client()
    http.force_login(operator)
    response = http.post(
        "/admin/tenants/tenant/add/",
        {
            "name": "Firma pelo admin",
            "slug": "firma-pelo-admin",
            "plan": "standard",
            "is_active": "on",
            "features": "cycles_r1,firm_settings_r1",
            "active_client_limit": "",
        },
        headers={"host": "localhost"},
    )
    assert response.status_code == HTTPStatus.FOUND, response.content.decode()[:2000]
    tenant = Tenant.objects.get(slug="firma-pelo-admin")
    with tenant_context(tenant.pk):
        account = BillingAccount.objects.get()
    assert (account.provider, account.status, account.version) == (
        "manual",
        BillingStatus.ACTIVE,
        1,
    )

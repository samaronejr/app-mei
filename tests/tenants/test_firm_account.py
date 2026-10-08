"""Audited firm settings bind actor, tenant and rendered revision independently."""

from datetime import date
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.db import connection
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import Event, PlatformEvent
from apps.authz.firm_account import can_manage_account
from apps.core.tenancy import tenant_context
from apps.tenants.branding_updates import store_logo
from apps.tenants.models import (
    BillingAccount,
    BillingStatus,
    FirmMessageState,
    FirmOutboundMessage,
    Invite,
    NotificationPreference,
    Tenant,
    TenantRole,
)
from apps.tenants.onboarding import (
    DEFAULT_FIRM_FEATURES,
    BillingChange,
    FirmRegistration,
    create_firm,
    update_billing,
)
from apps.tenants.services import (
    FirmProfile,
    TenantConflictError,
    save_notification_preference,
    save_profile,
)
from tests.browser.harness import mfa_session_cookie
from tests.support import enrol_totp
from tests.tenants.test_branding import PNG
from tests.tenants.test_cancellation_retention import enabled_firm
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)


def test_platform_onboarding_commits_invite_outbox_and_audit_together() -> None:
    operator = User.objects.create_user(
        email="operator@onboard.example.com", is_staff=True, is_superuser=True
    )
    tenant = create_firm(
        operator,
        FirmRegistration("Firma Nova", "firm-onboard", "owner@new.example.com", "A", 5),
    )
    assert tenant.features == list(DEFAULT_FIRM_FEATURES)
    assert tenant.active_client_limit == 5
    invite = Invite.objects.get(tenant=tenant)
    assert invite.role == TenantRole.OWNER
    with tenant_context(tenant.pk):
        message = FirmOutboundMessage.objects.get(invite=invite)
        assert (message.state, message.fence) == (FirmMessageState.QUEUED, 0)
        assert BillingAccount.objects.get().plan_band == "A"
    event = PlatformEvent.objects.get(tenant_id=tenant.pk)
    assert event.metadata["flow"] == "new_firm"
    assert event.metadata["invite_id"] == str(invite.pk)


def test_firm_owner_cannot_use_the_platform_creation_service() -> None:
    firm = enabled_firm("platform-owner-denied")
    with pytest.raises(PermissionDenied):
        create_firm(
            firm.owner, FirmRegistration("Denied", "denied-firm", "new@example.com")
        )
    assert not Tenant.objects.filter(slug="denied-firm").exists()


def test_invalid_onboarding_leaves_no_firm_invite_or_event() -> None:
    operator = User.objects.create_user(
        email="operator@invalid.example.com", is_staff=True, is_superuser=True
    )
    with pytest.raises(ValidationError):
        create_firm(
            operator, FirmRegistration("Invalid", "invalid-firm", "not-an-email")
        )
    assert not Tenant.objects.filter(slug="invalid-firm").exists()
    assert not PlatformEvent.objects.filter(actor=operator).exists()


def test_profile_and_personal_preference_have_independent_stale_fences() -> None:
    firm = enabled_firm("settings-stale")
    with tenant_context(firm.tenant.pk):
        saved = save_profile(
            firm.owner, firm.tenant.pk, FirmProfile("Current"), expected_version=1
        )
        with pytest.raises(TenantConflictError):
            save_profile(
                firm.owner,
                firm.tenant.pk,
                FirmProfile("Lost update"),
                expected_version=1,
            )
        preference = save_notification_preference(
            firm.owner, firm.tenant.pk, enabled=True, expected_version=0
        )
        with pytest.raises(TenantConflictError):
            save_notification_preference(
                firm.owner, firm.tenant.pk, enabled=False, expected_version=0
            )
        saved.refresh_from_db()
        preference.refresh_from_db()
        assert (saved.name, saved.settings_version) == ("Current", 2)
        assert (preference.daily_summary, preference.version) == (True, 1)
        assert Event.objects.filter(action="tenant.profile_updated").count() == 1
        assert not FirmOutboundMessage.objects.exists()


def test_staff_can_save_only_their_preference_and_never_costs() -> None:
    firm = enabled_firm("settings-staff")
    http = firm.as_accountant()
    with tenant_context(firm.tenant.pk):
        BillingAccount.objects.create(tenant=firm.tenant, status=BillingStatus.PAST_DUE)
        with pytest.raises(PermissionDenied):
            save_profile(
                firm.accountant,
                firm.tenant.pk,
                FirmProfile("No"),
                expected_version=1,
            )
        save_notification_preference(
            firm.accountant, firm.tenant.pk, enabled=True, expected_version=0
        )
        assert NotificationPreference.objects.get().user == firm.accountant
    body = http.get(reverse("firm-settings")).content.decode()
    assert "data-billing-past-due" not in body
    assert 'name="brand_name"' not in body
    assert http.get(reverse("firm-account")).status_code == HTTPStatus.FORBIDDEN
    assert (
        http.post(reverse("account-export-request"), {"version": 1}).status_code == 403
    )


def test_operations_admin_can_edit_branding_but_cannot_view_account_costs() -> None:
    firm = enabled_firm("settings-operations")
    operator = add_member(
        firm.tenant, "ops@settings.example.com", TenantRole.OPERATIONS_ADMIN
    )
    with tenant_context(firm.tenant.pk):
        save_profile(
            operator, firm.tenant.pk, FirmProfile("Ops edit"), expected_version=1
        )
        assert not can_manage_account(operator, firm.tenant.pk)
    http = firm.sign_in(operator)
    assert http.get(reverse("firm-settings")).status_code == HTTPStatus.OK
    assert http.get(reverse("firm-account")).status_code == HTTPStatus.FORBIDDEN


def test_superuser_without_an_owner_seat_cannot_substitute_for_owner() -> None:
    firm = enabled_firm("settings-live-owner")
    superuser = User.objects.create_user(
        email="super@outside.example.com", is_staff=True, is_superuser=True
    )
    with tenant_context(firm.tenant.pk):
        assert not can_manage_account(superuser, firm.tenant.pk)


def test_manual_billing_can_restore_suspension_but_refuses_stale_overwrites() -> None:
    firm = enabled_firm("billing-stale")
    operator = User.objects.create_user(
        email="operator@billing.example.com", is_staff=True, is_superuser=True
    )
    account = update_billing(
        operator,
        firm.tenant.pk,
        BillingChange("Manual", BillingStatus.SUSPENDED, None, 0, 0),
    )
    restored = update_billing(
        operator,
        firm.tenant.pk,
        BillingChange("Manual", BillingStatus.ACTIVE, None, 4, account.version),
    )
    with pytest.raises(TenantConflictError):
        update_billing(
            operator,
            firm.tenant.pk,
            BillingChange("Old", BillingStatus.PAST_DUE, None, 1, account.version),
        )
    with tenant_context(firm.tenant.pk):
        restored.refresh_from_db()
        assert (restored.status, restored.version) == (BillingStatus.ACTIVE, 2)
    firm.tenant.refresh_from_db()
    assert firm.tenant.active_client_limit == 4


def test_dates_render_as_native_input_values_in_billing_and_cancellation() -> None:
    firm = enabled_firm("settings-date-inputs")
    operator = User.objects.create_user(
        email="operator@dates.example.com", is_staff=True, is_superuser=True
    )
    update_billing(
        operator,
        firm.tenant.pk,
        BillingChange("Manual", BillingStatus.ACTIVE, date(2026, 11, 7), 4, 0),
    )
    platform = Client(SERVER_NAME="localhost")
    enrol_totp(operator)
    cookie = mfa_session_cookie(operator, "http://localhost")
    assert "name" in cookie
    assert "value" in cookie
    platform.cookies[cookie["name"]] = cookie["value"]
    response = platform.get(
        reverse("admin:tenants_tenant_billing", args=[firm.tenant.pk])
    )
    assert response.status_code == HTTPStatus.OK
    assert 'value="2026-11-07"' in response.content.decode()
    firm.tenant.cancellation_ends_on = date(2026, 11, 30)
    firm.tenant.save(update_fields=["cancellation_ends_on"])
    response = firm.as_owner().get(reverse("firm-account"))
    assert response.status_code == HTTPStatus.OK
    assert 'value="2026-11-30"' in response.content.decode()


def test_logo_storage_occurs_after_commit_and_preserves_newer_profile(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MEDIA_ROOT = tmp_path
    firm = enabled_firm("logo-phases")
    actual_save = default_storage.save
    phases: list[bool] = []

    def save(name: str, content: File[bytes], max_length: int | None = None) -> str:
        phases.append(connection.in_atomic_block)
        return actual_save(name, content, max_length=max_length)

    monkeypatch.setattr(default_storage, "save", save)
    with tenant_context(firm.tenant.pk):
        save_profile(
            firm.owner,
            firm.tenant.pk,
            FirmProfile("Logo profile", logo=PNG),
            expected_version=1,
        )
        assert phases == []
    firm.tenant.refresh_from_db()
    assert phases == [False]
    assert not firm.tenant.logo_upload_pending
    assert firm.tenant.logo.name
    current_key = firm.tenant.logo.name
    store_logo(firm.tenant.pk, firm.owner.pk, uuid4(), PNG)
    firm.tenant.refresh_from_db()
    assert (firm.tenant.name, firm.tenant.logo.name) == ("Logo profile", current_key)
    assert default_storage.exists(current_key)
    assert list((tmp_path / "branding" / str(firm.tenant.pk)).glob("*/logo.png")) == [
        tmp_path / current_key
    ]


def test_a_foreign_tenant_profile_command_is_not_a_tenant_selector() -> None:
    first = enabled_firm("settings-first")
    second = enabled_firm("settings-second")
    http = first.as_owner()
    response = http.post(
        reverse("firm-settings"),
        {"name": "First edited", "version": 1, "tenant_id": str(second.tenant.pk)},
    )
    assert response.status_code == HTTPStatus.FOUND
    first.tenant.refresh_from_db()
    second.tenant.refresh_from_db()
    assert first.tenant.name == "First edited"
    assert second.tenant.name != "First edited"

"""AXRM-008: every role of the six-role matrix meets the authority routes over HTTP."""

from collections.abc import Callable

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, TenantRole
from tests.integrations.test_authority import authority_api
from tests.ui.factories import Firm, add_member, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)
CAPABILITY = "fiscal.authority.manage"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"


def _firm(slug: str) -> Firm:
    firm = make_firm(slug, client_count=1)
    firm.tenant.features = ["cycles_r1", "scan_required", "federal_checks"]
    firm.tenant.save(update_fields=["features"])
    return firm


def _staff(firm: Firm) -> Client:
    assign(firm, firm.clients[0], firm.accountant)
    return firm.as_accountant()


def _client_role(role: str) -> Callable[[Firm], Client]:
    def session(firm: Firm) -> Client:
        user = add_member(
            firm.tenant,
            f"{role}@{firm.tenant.slug}.example.com",
            role,
            client=firm.clients[0],
        )
        return firm.sign_in(user)

    return session


def _platform_admin(firm: Firm) -> Client:
    admin = add_member(
        firm.tenant, f"pa@{firm.tenant.slug}.example.com", TenantRole.OWNER
    )
    session = firm.sign_in(admin)
    # The platform admin holds no firm membership (A7.6 PA = NONE): drop the seat that
    # let the account sign in, keep the authenticated session.
    Membership.objects.filter(user=admin).delete()
    User.objects.filter(pk=admin.pk).update(is_superuser=True, is_staff=True)
    return session


def _ops(firm: Firm) -> Client:
    ops = add_member(
        firm.tenant,
        f"ops@{firm.tenant.slug}.example.com",
        TenantRole.OPERATIONS_ADMIN,
    )
    return firm.sign_in(ops)


def _post_all(browser: Client, firm: Firm) -> list[int]:
    base = f"/clientes/{firm.clients[0].pk}/autoridades/sitfis"
    declared = browser.post(
        f"{base}/declarar/",
        {
            "expected_version": 0,
            "code": "00002",
            "valid_from": "2026-01-01",
            "evidence": SimpleUploadedFile("procuracao.pdf", PDF, "application/pdf"),
        },
    )
    verified = browser.post(f"{base}/verificar/", {"expected_version": 0})
    revoked = browser.post(f"{base}/revogar/", {"expected_version": 0})
    return [declared.status_code, verified.status_code, revoked.status_code]


def _rows(firm: Firm) -> tuple[int, int]:
    with tenant_context(firm.tenant.pk):
        return (
            apps.get_model("integrations", "AuthorityRecord").objects.count(),
            apps.get_model("obligations", "Document").objects.count(),
        )


def _refusals(firm: Firm) -> int:
    return PlatformEvent.objects.filter(
        action=AuditAction.DENIED,
        tenant_id=firm.tenant.pk,
        metadata__capability=CAPABILITY,
    ).count()


def test_staff_accountant_is_refused_at_the_view_gate_before_any_upload(
    settings: SettingsWrapper,
) -> None:
    authority_api()
    settings.CLAMAV_ENABLED = True
    firm = _firm("access-staff")
    browser = _staff(firm)
    assert _post_all(browser, firm) == [403, 403, 403]
    assert _rows(firm) == (0, 0)
    # Only the view's require_can records the refusal; the service re-check alone
    # would answer 403 too, but silently and after reading the evidence upload.
    assert _refusals(firm) == 3


@pytest.mark.parametrize(
    ("label", "session"),
    [
        ("client_owner", _client_role(TenantRole.CLIENT_OWNER)),
        ("client_collaborator", _client_role(TenantRole.CLIENT_COLLABORATOR)),
        ("platform_admin", _platform_admin),
    ],
)
def test_roles_without_the_capability_cannot_write_authority(
    settings: SettingsWrapper,
    label: str,
    session: Callable[[Firm], Client],
) -> None:
    authority_api()
    settings.CLAMAV_ENABLED = True
    firm = _firm(f"access-{label.replace('_', '-')}")
    statuses = _post_all(session(firm), firm)
    assert all(status in {403, 404} for status in statuses), (label, statuses)
    assert _rows(firm) == (0, 0)


@pytest.mark.parametrize(
    ("label", "session"), [("owner", Firm.as_owner), ("ops", _ops)]
)
def test_holders_of_the_capability_reach_the_service(
    settings: SettingsWrapper,
    label: str,
    session: Callable[[Firm], Client],
) -> None:
    authority_api()
    settings.CLAMAV_ENABLED = True
    firm = _firm(f"access-{label}")
    browser = session(firm)
    base = f"/clientes/{firm.clients[0].pk}/autoridades/sitfis"
    response = browser.post(
        f"{base}/declarar/",
        {"expected_version": 0, "code": "00002", "valid_from": "2026-01-01"},
    )
    assert response.status_code == 302
    assert _rows(firm)[0] == 1
    assert _refusals(firm) == 0

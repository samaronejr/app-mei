"""AXRM-008: every role of the six-role matrix meets the authority routes over HTTP."""

from collections.abc import Callable

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.authz.models import GrantLevel, RoleGrant
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


def _refusals(firm: Firm) -> int:
    return PlatformEvent.objects.filter(
        action=AuditAction.DENIED,
        tenant_id=firm.tenant.pk,
        metadata__capability=CAPABILITY,
    ).count()


def _post_all(browser: Client, firm: Firm) -> list[tuple[int, int]]:
    """POST declare, verify and revoke; return (status, refusals written) each."""
    base = f"/clientes/{firm.clients[0].pk}/autoridades/sitfis"
    requests = (
        (
            f"{base}/declarar/",
            {
                "expected_version": 0,
                "code": "00002",
                "valid_from": "2026-01-01",
                "evidence": SimpleUploadedFile(
                    "procuracao.pdf", PDF, "application/pdf"
                ),
            },
        ),
        (f"{base}/verificar/", {"expected_version": 0}),
        (f"{base}/revogar/", {"expected_version": 0}),
    )
    outcomes = []
    for path, data in requests:
        before = _refusals(firm)
        response = browser.post(path, data)
        outcomes.append((response.status_code, _refusals(firm) - before))
    return outcomes


def _rows(firm: Firm) -> tuple[int, int]:
    with tenant_context(firm.tenant.pk):
        return (
            apps.get_model("integrations", "AuthorityRecord").objects.count(),
            apps.get_model("obligations", "Document").objects.count(),
        )


def test_staff_accountant_is_refused_at_the_view_gate_before_any_upload(
    settings: SettingsWrapper,
) -> None:
    authority_api()
    settings.CLAMAV_ENABLED = True
    firm = _firm("access-staff")
    browser = _staff(firm)
    # Exactly one durable refusal per request. Only the view's require_can records
    # it; the service re-check alone would answer 403 too, but silently and after
    # reading the evidence upload.
    assert _post_all(browser, firm) == [(403, 1), (403, 1), (403, 1)]
    assert _rows(firm) == (0, 0)


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
    browser = session(firm)
    outcomes = _post_all(browser, firm)
    # Refused before any view runs (no firm seat), yet each refusal is on record:
    # exactly one DENIED event per request.
    assert outcomes == [(403, 1), (403, 1), (403, 1)], (label, outcomes)
    assert _rows(firm) == (0, 0)
    # Recording the refusal admits nothing: a require_can view reached without a
    # seat still refuses, shows no client data and records its own capability.
    before = PlatformEvent.objects.filter(
        action=AuditAction.DENIED, metadata__capability="clients.view_assigned"
    ).count()
    page = browser.get(f"/clientes/{firm.clients[0].pk}/")
    assert page.status_code == 403
    assert firm.clients[0].legal_name.encode() not in page.content
    assert (
        PlatformEvent.objects.filter(
            action=AuditAction.DENIED,
            tenant_id=firm.tenant.pk,
            metadata__capability="clients.view_assigned",
        ).count()
        == before + 1
    )


@pytest.mark.parametrize(
    ("label", "session"), [("owner", Firm.as_owner), ("ops", _ops)]
)
def test_firm_roles_whose_grant_is_withdrawn_are_refused_on_record(
    settings: SettingsWrapper,
    label: str,
    session: Callable[[Firm], Client],
) -> None:
    authority_api()
    settings.CLAMAV_ENABLED = True
    firm = _firm(f"access-none-{label}")
    browser = session(firm)
    RoleGrant.objects.filter(
        role__in=[TenantRole.OWNER, TenantRole.OPERATIONS_ADMIN],
        capability__slug=CAPABILITY,
    ).update(level=GrantLevel.NONE)
    assert _post_all(browser, firm) == [(403, 1), (403, 1), (403, 1)]
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

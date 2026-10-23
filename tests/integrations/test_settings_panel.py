"""AXRM-007: the read-only "Conexoes fiscais" panel on the firm settings page.

Shown only to holders of `fiscal.connections.manage` (owner, operations admin) and only
while one of the regional flags is effectively on (which already requires `cycles_r1`
and `scan_required`). It lists the firm's own connections with their environment and
an effective status in which the platform pause wins, and it never paints a connection
green: a connection is configuration, not verified evidence (A7.7). Management actions
arrive with todo 34; this panel has no form.
"""

import re
from typing import Final

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations.models import ProviderConnection
from apps.tenants.models import Tenant, TenantRole
from tests.ui.factories import Firm, add_member, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

PANEL: Final = "data-fiscal-connections"
SERPRO_CREDENTIAL_LABEL: Final = "serpro_contratante"
REGIONAL: Final = ("cycles_r1", "firm_settings_r1", "scan_required", "federal_checks")


def _flags(tenant: Tenant, *flags: str) -> None:
    tenant.features = list(flags)
    tenant.save(update_fields=["features"])


def _connect(tenant: Tenant, **fields: str) -> ProviderConnection:
    values = {"provider": "serpro", "environment": "fake", "status": "ativa"}
    values.update(fields)
    with tenant_context(tenant.pk):
        return ProviderConnection.objects.create(tenant=tenant, **values)


def _page(client: Client) -> str:
    response = client.get("/configuracoes/")
    assert response.status_code == 200
    return response.content.decode()


@pytest.fixture
def firm() -> Firm:
    built = make_firm("conexoes")
    _flags(built.tenant, *REGIONAL)
    return built


def _panel(html: str) -> str:
    match = re.search(rf"<section[^>]*{PANEL}.*?</section>", html, re.DOTALL)
    assert match, "the fiscal connections panel did not render"
    return match.group(0)


def test_the_owner_sees_each_connection_with_environment_and_paused_status(
    firm: Firm,
) -> None:
    _connect(firm.tenant, secret_ref=SERPRO_CREDENTIAL_LABEL)
    _connect(firm.tenant, provider="focus", environment="homologacao", status="inativa")

    panel = _panel(_page(firm.as_owner()))

    assert "<caption" in panel
    assert panel.count('scope="col"') == 3
    assert "SERPRO" in panel
    assert "Focus" in panel
    assert "Simulado" in panel
    assert "Homologação" in panel
    # PROVIDERS_PAUSED ships True, so an `ativa` connection reads as paused.
    assert "Pausada pela plataforma" in panel
    assert 'data-state="confirmed"' not in panel
    # The secret's name is operator configuration, not firm-facing copy.
    assert SERPRO_CREDENTIAL_LABEL not in panel
    assert "<form" not in panel


def test_an_unpaused_active_connection_is_still_not_painted_as_verified(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    settings.PROVIDERS_PAUSED = False
    _connect(firm.tenant)

    panel = _panel(_page(firm.as_owner()))

    assert "Ativa" in panel
    assert "Pausada pela plataforma" not in panel
    assert 'data-state="confirmed"' not in panel


def test_a_provider_pause_wins_over_an_active_row(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    settings.PROVIDERS_PAUSED = False
    settings.FOCUS_PAUSED = True
    _connect(firm.tenant, provider="focus")

    assert "Pausada pela plataforma" in _panel(_page(firm.as_owner()))


def test_the_operations_admin_sees_the_panel(firm: Firm) -> None:
    ops = add_member(
        firm.tenant, "ops@conexoes.example.com", TenantRole.OPERATIONS_ADMIN
    )
    _connect(firm.tenant)

    assert "SERPRO" in _panel(_page(firm.sign_in(ops)))


def test_a_staff_accountant_does_not_see_the_panel(firm: Firm) -> None:
    _connect(firm.tenant)

    assert PANEL not in _page(firm.as_accountant())


def test_the_panel_is_absent_while_no_regional_flag_is_on(firm: Firm) -> None:
    _connect(firm.tenant)
    _flags(firm.tenant, "cycles_r1", "firm_settings_r1", "scan_required")

    assert PANEL not in _page(firm.as_owner())


def test_the_panel_is_absent_while_scanning_is_not_required(firm: Firm) -> None:
    _connect(firm.tenant)
    _flags(firm.tenant, "cycles_r1", "firm_settings_r1", "federal_checks")

    assert PANEL not in _page(firm.as_owner())


def test_an_empty_panel_says_what_happens_next(firm: Firm) -> None:
    panel = _panel(_page(firm.as_owner()))

    assert 'data-ui-state="empty"' in panel
    assert "Nenhuma conexão fiscal configurada" in panel


def test_another_firms_connection_is_never_listed(firm: Firm) -> None:
    other = make_firm("vizinha")
    _connect(other.tenant, provider="focus", environment="producao")
    _connect(firm.tenant)

    panel = _panel(_page(firm.as_owner()))

    assert "Produção" not in panel
    assert "Focus" not in panel

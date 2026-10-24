"""Billing classification and reservation lifecycle across retries and crashes."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import AdmissionDeniedError
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.models import ClientProviderLink
from apps.tenants.models import UsageKind, UsageRecord
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_budget_caps import (
    budget_link,
    paid_request,
    provider_link,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["budget_link", "provider_link"]


@pytest.mark.parametrize("status", [200, 202, 403])
def test_serpro_billed_status_settles_even_errors(
    budget_link: ClientProviderLink, status: int
) -> None:
    # Given a reserved Consulta, regardless of adapter success/error wording.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    # When SERPRO returns a billable response.
    with ProviderPeer(status=status) as peer:
        result = perform(ticket, peer)
    # Then liability and confirmed daily usage cannot hide billed failures.
    assert result.billed == "sim"
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.known_liability, budget.reserved) == (240_000, 0)
        usage = UsageRecord.objects.get(metric="serpro_consulta")
        assert usage.quantity == 1
        assert usage.kind == UsageKind.CONFIRMED
        assert usage.source_event == call.pk


@pytest.mark.parametrize("status", [204, 304, 400, 401, 404, 429, 500, 503, 504])
def test_unbilled_status_releases_reservation(
    budget_link: ClientProviderLink, status: int
) -> None:
    # Given a reserved call whose test peer deliberately claims every response billed.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    # When the authoritative billing status says it is not chargeable.
    with ProviderPeer(status=status) as peer:
        result = perform(ticket, peer)
    # Then the billing contract, not the peer's hint, determines liability.
    assert result.billed == "nao"
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.known_liability, budget.uncertain_exposure, budget.reserved) == (
            0,
            0,
            0,
        )
        assert not UsageRecord.objects.filter(metric="serpro_consulta").exists()


def test_lost_response_retains_maximum_exposure(
    budget_link: ClientProviderLink,
) -> None:
    # Given a provider that consumes the request and resets before returning a status.
    call = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    # When the transport loses the outcome.
    with ProviderPeer(reset=True) as peer:
        perform(ticket, peer)
    # Then the amount is uncertain, not refunded.
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.known_liability, budget.uncertain_exposure, budget.reserved) == (
            0,
            240_000,
            0,
        )
        assert ProviderReservation.objects.get().state == "uncertain"


@pytest.mark.parametrize(
    "prices", [{}, {"serpro_consulta": "NaN"}, {"serpro_consulta": "-1"}]
)
def test_missing_or_invalid_price_refuses_before_reservation(
    budget_link: ClientProviderLink,
    settings: SettingsWrapper,
    prices: dict[str, str],
) -> None:
    # Given no usable tariff entry.
    settings.USAGE_UNIT_PRICES_BRL = prices
    # When a call is requested.
    with pytest.raises(AdmissionDeniedError):
        reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    # Then no exposure was admitted as a free call.
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0
        assert not ProviderReservation.objects.exists()


def test_missing_platform_cap_refuses_as_missing_cap(
    budget_link: ClientProviderLink, settings: SettingsWrapper
) -> None:
    # Given a tenant cap but no platform-wide SERPRO ceiling (the closed default).
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {}
    # When a priced call is requested.
    with pytest.raises(AdmissionDeniedError, match="cota_ausente"):
        reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    # Then the refusal names the missing ceiling, not a missing tariff.
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0
        assert not ProviderReservation.objects.exists()


def test_month_rollover_does_not_reuse_old_cap(
    budget_link: ClientProviderLink, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a cap provisioned for this month only.
    # When the next month arrives.
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime(2030, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(AdmissionDeniedError):
        reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    # Then absence of a new cap is disabled, never unlimited.
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0


@pytest.mark.parametrize("operation", ["SP-SIT-P", "SYN-MONITORAR"])
def test_unbilled_endpoint_never_charges_even_a_billed_http_status(
    budget_link: ClientProviderLink, operation: str
) -> None:
    # Given an explicit unbilled Apoiar/Monitorar operation class.
    call_contracts.register_operation(
        replace(
            call_contracts.operation_for("SP-PAG"),
            code=operation,
            billing_class="nao_faturada",
            operation_class="issuance",
        )
    )
    # When the endpoint returns 403.
    call = reserve(
        budget_link.tenant_id,
        budget_link.pk,
        replace(paid_request(operation), operation=operation),
    )
    ticket = claim(budget_link.tenant_id, call.pk)
    assert ticket is not None
    with ProviderPeer(status=403) as peer:
        result = perform(ticket, peer)
    # Then only Consulta/Emissao are charged, not status codes in isolation.
    assert result.billed == "nao"
    with tenant_context(budget_link.tenant_id):
        assert ProviderBudget.objects.get().known_liability == 0
        assert not UsageRecord.objects.exists()

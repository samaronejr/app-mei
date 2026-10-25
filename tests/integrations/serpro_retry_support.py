"""Real budget, admission, transport and journal helpers for the 401 exception."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts
from apps.integrations.budget import BudgetPolicy
from apps.integrations.budget_models import ProviderBudget
from apps.integrations.call_contracts import CallRequest, CallResult, Claim
from apps.integrations.call_dispatch import Adapter, Dispatch
from apps.integrations.call_models import ProviderCall
from apps.integrations.call_protocol import claim, reserve
from apps.integrations.models import ClientProviderLink
from apps.integrations.transport import ProviderTransport
from tests.integrations.serpro_support import CONTRATANTE, FIRM, serpro_module
from tests.integrations.test_call_protocol import provider_link

__all__ = ["provider_link", "spend_link"]


@pytest.fixture
def spend_link(
    provider_link: ClientProviderLink, settings: SettingsWrapper
) -> ClientProviderLink:
    settings.USAGE_UNIT_PRICES_BRL = {
        "serpro_consulta": "0.24",
        "serpro_emissao": "0.24",
    }
    settings.PROVIDER_GLOBAL_MONTHLY_CAP_BRL = {"serpro": "10.00"}
    call_contracts._OPERATIONS["SP-DAS"] = replace(
        call_contracts.operation_for("SP-DAS"),
        policy=BudgetPolicy(lambda call: True),
    )
    with tenant_context(provider_link.tenant_id):
        ProviderBudget.objects.create(
            tenant_id=provider_link.tenant_id,
            provider="serpro",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=2_000_000,
            tariff_revision="synthetic-reauth-v1",
        )
    return provider_link


def client_dispatch(
    link: ClientProviderLink, origin: str
) -> tuple[ProviderCall, Claim, Adapter]:
    module = serpro_module("adapter")
    parties = module.Parties(CONTRATANTE, FIRM, link.client.cnpj)
    data = {"periodoApuracao": "202610"}
    wire = module.build_request("SP-DAS", parties, data)
    request = CallRequest("SP-DAS", "a" * 64, sha256(wire.body).hexdigest(), "b" * 64)
    row = reserve(link.tenant_id, link.pk, request)
    ticket = claim(link.tenant_id, row.pk)
    assert ticket is not None
    adapter = module.SerproAdapter(
        ProviderTransport("serpro", "fake", origin), parties, data
    )
    return row, ticket, adapter


@dataclass(frozen=True, slots=True)
class AfterFirstResponse:
    adapter: Adapter
    change_controls: Callable[[], None]

    def send(self, dispatch: Dispatch) -> CallResult:
        result = self.adapter.send(dispatch)
        if dispatch.receipt.attempt_number == 1:
            self.change_controls()
        return result


@dataclass(frozen=True, slots=True)
class TracedAdapter:
    adapter: Adapter
    claims: list[Claim] = field(default_factory=list)

    def send(self, dispatch: Dispatch) -> CallResult:
        self.claims.append(dispatch.claim)
        return self.adapter.send(dispatch)

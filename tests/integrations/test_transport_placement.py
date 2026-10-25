"""The hardened wire keeps the placed firm and budget protocols usable."""

from dataclasses import dataclass
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import CallResult, StaleClaimError
from apps.integrations.call_dispatch import Dispatch, transmit
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import begin_io, claim, reserve
from apps.integrations.firm_calls import FirmDispatch, firm_claim, firm_perform
from apps.integrations.models import ClientProviderLink, FirmAuthorization
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import EnvioNaoAutorizado
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.test_budget_caps import (
    budget_link,
    paid_request,
    provider_link,
)
from tests.integrations.test_firm_termo_journal import (
    observations,
    reload,
    termo,
)
from tests.integrations.test_firm_termo_journal import (
    reserve as reserve_termo,
)
from tests.integrations.test_transport import serpro_request

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["budget_link", "provider_link", "termo"]


@dataclass(frozen=True, slots=True)
class TermoSender:
    origin: str

    def send(self, dispatch: FirmDispatch) -> CallResult:
        reply = ProviderTransport("serpro", "fake", self.origin).send(
            "SP-TERMO", serpro_request("ENVIOXMLASSINADO81", "AUTENTICAPROCURADOR")
        )
        return CallResult(CallState.SUCCESS, reply.status, "ok", "nao")


@dataclass(frozen=True, slots=True)
class PaidSender:
    origin: str

    def send(self, dispatch: Dispatch) -> CallResult:
        reply = ProviderTransport("serpro", "fake", self.origin).send(
            "SP-PAG", serpro_request()
        )
        return CallResult(CallState.SUCCESS, reply.status, "ok", "sim")


def test_placed_firm_dispatch_can_use_the_hardened_transport(
    termo: FirmAuthorization,
) -> None:
    # Given a committed, journaled firm SP-TERMO claim with no client call.
    row = reserve_termo(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    with FakeSerpro((Reply(),)) as peer:
        # When the firm protocol invokes the hardened provider transport.
        result = None
        refused = False
        try:
            result = firm_perform(ticket, TermoSender(peer.origin))
        except EnvioNaoAutorizado:
            refused = True
        # Then exactly one request and the firm result are retained.
        assert len(peer.requests) == 1
        assert not refused
    assert result is not None
    assert result.state == CallState.SUCCESS
    assert reload(termo, row.pk).state == CallState.SUCCESS
    assert observations(termo, row.pk) == [("result", CallState.SUCCESS)]


@pytest.mark.parametrize("reconcile_first", [False, True])
def test_refused_transmit_releases_its_started_budget_reservation(
    budget_link: ClientProviderLink, reconcile_first: bool
) -> None:
    # Given a paid, started attempt that has not sent a byte.
    row = reserve(budget_link.tenant_id, budget_link.pk, paid_request())
    ticket = claim(budget_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with tenant_context(budget_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    if reconcile_first:
        assert claim(budget_link.tenant_id, row.pk) is None
    with FakeSerpro((Reply(),)) as peer:
        # When the live-claim recheck refuses transmission.
        with pytest.raises(StaleClaimError):
            transmit(dispatch, PaidSender(peer.origin))
        # Then the consumed dispatch cannot send on a later retry either.
        with pytest.raises(StaleClaimError):
            transmit(dispatch, PaidSender(peer.origin))
        assert peer.requests == []
    with tenant_context(budget_link.tenant_id):
        budget = ProviderBudget.objects.get()
        assert (budget.reserved, budget.uncertain_exposure, budget.known_liability) == (
            0,
            0,
            0,
        )
        assert ProviderReservation.objects.get().state == "released"

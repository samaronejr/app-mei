"""Wire outcomes finalize the claimed call; only an issued dispatch opens the wire."""

from dataclasses import dataclass, replace

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import CallResult, StaleClaimError
from apps.integrations.call_dispatch import Dispatch, record_result, transmit
from apps.integrations.call_protocol import begin_io, claim, finalize, reserve
from apps.integrations.models import ClientProviderLink
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import OperacaoNaoPermitida, TransportFailure
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.test_call_protocol import reload_call, request
from tests.integrations.test_transport import serpro_request

pytestmark = pytest.mark.django_db(transaction=True)
pytest_plugins = ("tests.integrations.test_call_protocol",)


@dataclass(frozen=True, slots=True)
class _Probe:
    origin: str

    def send(self, dispatch: Dispatch) -> CallResult:
        try:
            response = ProviderTransport("serpro", "fake", self.origin).send(
                "SP-DAS", serpro_request("GERARDASPDF21", "PGMEI")
            )
        except TransportFailure as error:
            return error.result()
        return CallResult("sucesso", response.status, "ok", "nao")


def test_oversize_is_finalized_as_failed_without_parsing_or_retry(
    provider_link: ClientProviderLink,
) -> None:
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with FakeSerpro((Reply(body=b"x" * (6 * 1024 * 1024)),)) as peer:
        result = transmit(dispatch, _Probe(peer.origin))
        record_result(dispatch, result)
        assert finalize(ticket, result)
        assert len(peer.requests) == 1
    saved = reload_call(provider_link, row.pk)
    assert saved.state == "falhou"
    assert saved.provider_code == "resposta_excede_limite"


@dataclass(frozen=True, slots=True)
class _OtherOperation:
    origin: str

    def send(self, dispatch: Dispatch) -> CallResult:
        try:
            ProviderTransport("serpro", "fake", self.origin).send(
                "SP-PAG", serpro_request()
            )
        except OperacaoNaoPermitida:
            return CallResult("falhou", None, "envio_nao_autorizado", "nao")
        return CallResult("sucesso", 200, "ok", "nao")


def test_one_dispatch_opens_the_wire_exactly_once(
    provider_link: ClientProviderLink,
) -> None:
    # Given a claimed, fenced and journaled SP-DAS dispatch.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with FakeSerpro((Reply(), Reply())) as peer:
        # When the same dispatch is transmitted a second time.
        transmit(dispatch, _Probe(peer.origin))
        replay_refused = False
        try:
            transmit(dispatch, _Probe(peer.origin))
        except StaleClaimError:
            replay_refused = True
        # Then only the first transmission reached the provider.
        assert len(peer.requests) == 1
        assert replay_refused


def test_dispatch_cannot_carry_a_different_operation(
    provider_link: ClientProviderLink,
) -> None:
    # Given an authorized SP-DAS dispatch.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with FakeSerpro((Reply(),)) as peer:
        # When its adapter tries to send another catalog operation.
        result = transmit(dispatch, _OtherOperation(peer.origin))
        # Then nothing reaches the provider.
        assert peer.requests == []
        assert result.provider_code == "envio_nao_autorizado"


def test_issued_key_with_a_forged_receipt_cannot_open_the_wire(
    provider_link: ClientProviderLink,
) -> None:
    # Given an authorized fake-environment dispatch.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    forged = replace(
        dispatch, receipt=replace(dispatch.receipt, environment="producao")
    )
    with FakeSerpro((Reply(),)) as peer:
        # When a copy claiming the production environment is transmitted.
        refused = False
        try:
            transmit(forged, _Probe(peer.origin))
        except StaleClaimError:
            refused = True
        # Then it is refused and the genuine dispatch still transmits once.
        assert refused
        assert peer.requests == []
        transmit(dispatch, _Probe(peer.origin))
        assert len(peer.requests) == 1


def test_transport_refuses_io_inside_transaction(
    provider_link: ClientProviderLink,
) -> None:
    with FakeSerpro(()) as peer:
        with tenant_context(provider_link.tenant_id), pytest.raises(RuntimeError):
            ProviderTransport("serpro", "fake", peer.origin).send(
                "SP-DAS", serpro_request("GERARDASPDF21", "PGMEI")
            )
        assert not peer.requests

"""Wire outcomes finalize the claimed call; only an issued dispatch opens the wire."""

from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations import call_dispatch
from apps.integrations.call_contracts import CallResult, StaleClaimError
from apps.integrations.call_dispatch import Dispatch, record_result, transmit
from apps.integrations.call_models import CallState, ProviderCall
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


def test_expired_lease_after_begin_io_sends_nothing(
    provider_link: ClientProviderLink,
) -> None:
    # Given an issued dispatch whose lease then expires, and another worker
    # that finds it and marks the in-flight call uncertain (review 2, B1).
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=row.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    assert claim(provider_link.tenant_id, row.pk) is None
    assert reload_call(provider_link, row.pk).state == "incerta"
    with FakeSerpro((Reply(), Reply())) as peer:
        # When the original process transmits the dispatch.
        with pytest.raises(StaleClaimError):
            transmit(dispatch, _Probe(peer.origin))
        # Then no byte reached the provider, and the dispatch stays consumed.
        with pytest.raises(StaleClaimError):
            transmit(dispatch, _Probe(peer.origin))
        assert peer.requests == []


@pytest.mark.parametrize(
    "change",
    [
        "lease_until",
        "state",
        "fence",
        "lease_owner",
        "execution_epoch",
        "platform_epoch",
    ],
)
def test_dispatch_whose_claim_no_longer_holds_sends_nothing(
    provider_link: ClientProviderLink, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # Given an issued dispatch, after which one condition of its claim stops
    # holding (expired lease, another state, fence, owner or epoch).
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    dispatch = begin_io(ticket)
    if change == "platform_epoch":
        later = ticket.execution_epoch + 1
        monkeypatch.setattr(call_dispatch, "current_epoch", lambda: later)
    else:
        value = {
            "lease_until": timezone.now() - timedelta(seconds=1),
            "state": CallState.UNCERTAIN,
            "fence": ticket.fence + 1,
            "lease_owner": uuid4(),
            "execution_epoch": ticket.execution_epoch + 1,
        }[change]
        with tenant_context(provider_link.tenant_id):
            ProviderCall.objects.filter(pk=row.pk).update(**{change: value})
    with FakeSerpro((Reply(),)) as peer:
        # When it is transmitted.
        with pytest.raises(StaleClaimError):
            transmit(dispatch, _Probe(peer.origin))
        # Then nothing was sent.
        assert peer.requests == []

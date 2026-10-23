"""A bounded wire refusal is journaled and fenced into the real call row."""

from dataclasses import dataclass

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import CallResult
from apps.integrations.call_dispatch import Dispatch, record_result, transmit
from apps.integrations.call_protocol import begin_io, claim, finalize, reserve
from apps.integrations.models import ClientProviderLink
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import TransportFailure
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


def test_transport_refuses_io_inside_transaction(
    provider_link: ClientProviderLink,
) -> None:
    with FakeSerpro(()) as peer:
        with tenant_context(provider_link.tenant_id), pytest.raises(RuntimeError):
            ProviderTransport("serpro", "fake", peer.origin).send(
                "SP-DAS", serpro_request("GERARDASPDF21", "PGMEI")
            )
        assert not peer.requests

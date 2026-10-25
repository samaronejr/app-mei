"""Firm termo submissions receive the same single definitive-401 exception."""

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import FirmProviderCall, FirmProviderCallAttempt
from apps.integrations.firm_calls import firm_claim, firm_perform
from apps.integrations.models import FirmAuthorization
from apps.integrations.transport import ProviderTransport
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_support import CONTRATANTE, FIRM, serpro_module
from tests.integrations.test_firm_termo_journal import reserve, termo, termo_request

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["termo"]


@pytest.mark.parametrize(("second", "state"), [(200, "sucesso"), (401, "falhou")])
def test_firm_401_retry_is_fresh_bounded_and_clientless(
    termo: FirmAuthorization, second: int, state: str
) -> None:
    # Given one firm submission and a complete 401, then success or another 401.
    module = serpro_module("adapter")
    parties = module.Parties(CONTRATANTE, FIRM, FIRM)
    data = {"xml": "<termo-sintetico-assinado/>"}
    wire = module.build_request("SP-TERMO", parties, data)
    row = reserve(termo, termo_request(body=wire.body.decode()))
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    with FakeSerpro((Reply(status=401), Reply(status=second), Reply())) as peer:
        adapter = module.SerproAdapter(
            ProviderTransport("serpro", "fake", peer.origin),
            parties,
            data,
        )
        # When the public firm protocol, not the adapter, performs both attempts.
        result = firm_perform(ticket, adapter)
        # Then it remains one submission, without a fabricated client or third send.
        assert result.state == state
        assert len(peer.requests) == 2
        assert firm_claim(termo.tenant_id, row.pk) is None
    with tenant_context(termo.tenant_id):
        assert FirmProviderCall.objects.count() == 1
        saved = FirmProviderCall.objects.get(pk=row.pk)
        assert saved.attempts == saved.fence == 2
        assert saved.request_key == row.request_key
        assert list(
            FirmProviderCallAttempt.objects.filter(call=row, observation="result")
            .order_by("attempt_number")
            .values_list("attempt_number", "fence", "http_status")
        ) == [(1, 1, 401), (2, 2, second)]
        if second == 401:
            issues = serpro_module("models").FirmIntegrationIssue
            assert issues.objects.filter(
                call=row, reason="autenticacao_serpro_recusada", owner__isnull=False
            ).exists()
            assert "client" not in {field.name for field in issues._meta.get_fields()}


@pytest.mark.parametrize(
    "reply",
    [
        Reply(reset=True),
        Reply(status=401, body=b"longer-than-chunk", chunks=(b"x",)),
        Reply(status=503),
    ],
    ids=["reset", "partial-401", "503"],
)
def test_ambiguous_termo_submission_never_resends(
    termo: FirmAuthorization, reply: Reply
) -> None:
    # Given an upstream outcome which may have accepted the signed termo.
    module = serpro_module("adapter")
    parties = module.Parties(CONTRATANTE, FIRM, FIRM)
    data = {"xml": "<termo-sintetico-assinado/>"}
    wire = module.build_request("SP-TERMO", parties, data)
    row = reserve(termo, termo_request(body=wire.body.decode()))
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    with FakeSerpro((reply, Reply())) as peer:
        adapter = module.SerproAdapter(
            ProviderTransport("serpro", "fake", peer.origin),
            parties,
            data,
        )
        # When the phased firm protocol observes that ambiguity.
        result = firm_perform(ticket, adapter)
        # Then it is quarantined and cannot obtain another transmission slot.
        assert result.state == "incerta"
        assert len(peer.requests) == 1
        assert firm_claim(termo.tenant_id, row.pk) is None

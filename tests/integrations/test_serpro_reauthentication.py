"""A definitive 401 is the sole bounded exception to the no-resend protocol."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID

import pytest
from django.db import connection, connections
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper
from requests import Response

from apps.core.tenancy import tenant_context
from apps.integrations import call_protocol, transport_wire
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    Claim,
    StaleClaimError,
)
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.call_protocol import claim, perform
from apps.integrations.models import ClientProviderLink, IntegrationIssue
from apps.integrations.transport_types import ProviderResponse, TransportFailure
from apps.integrations.transport_wire import WireLimits
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.serpro_retry_support import (
    AfterFirstResponse,
    TracedAdapter,
    client_dispatch,
    provider_link,
    spend_link,
)
from tests.integrations.test_call_protocol import reload_call

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link", "spend_link"]


def test_401_then_200_uses_two_fences_journals_and_reservations(
    spend_link: ClientProviderLink,
) -> None:
    # Given a paid issuance whose first complete response rejects its stale token.
    with FakeSerpro((Reply(status=401), Reply(status=200))) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        # When the public protocol performs the call.
        result = perform(ticket, adapter)
        # Then each send has a fresh tag and the identical business request.
        assert result.state == "sucesso"
        assert len(peer.requests) == 2
        assert peer.requests[0].body == peer.requests[1].body
        tags = [
            {k.lower(): v for k, v in request.headers}["x-request-tag"]
            for request in peer.requests
        ]
        assert len(set(tags)) == 2
    with tenant_context(spend_link.tenant_id):
        assert list(
            ProviderCallAttempt.objects.filter(call=row, observation="result")
            .order_by("attempt_number")
            .values_list("attempt_number", "fence", "http_status", "billed")
        ) == [(1, 1, 401, "nao"), (2, 2, 200, "sim")]
        assert list(
            ProviderReservation.objects.filter(call=row)
            .order_by("attempt_number")
            .values_list("attempt_number", "state", "microbrl")
        ) == [(1, "released", 240_000), (2, "known", 240_000)]
        budget = ProviderBudget.objects.get()
        assert (budget.reserved, budget.known_liability, budget.uncertain_exposure) == (
            0,
            240_000,
            0,
        )
        assert ProviderCall.objects.get(pk=row.pk).request_key == row.request_key
        assert ProviderCall.objects.count() == 1
    saved = reload_call(spend_link, row.pk)
    assert saved.attempts == saved.fence == 2


def test_second_401_is_terminal_owned_work_and_never_a_third_send(
    spend_link: ClientProviderLink,
) -> None:
    # Given two definitive authentication refusals and a tempting third success.
    with FakeSerpro((Reply(status=401), Reply(status=401), Reply())) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        traced = TracedAdapter(adapter)
        # When the call exhausts its single reauthentication exception.
        result = perform(ticket, traced)
        # Then another claim cannot consume the third scripted response.
        assert result.state == "falhou"
        assert len(peer.requests) == 2
        assert claim(spend_link.tenant_id, row.pk) is None
        from apps.integrations.serpro.reauthentication import (  # noqa: PLC0415
            schedule_client_retry,
        )

        assert not schedule_client_retry(traced.claims[-1], result)
        assert claim(spend_link.tenant_id, row.pk) is None
    with tenant_context(spend_link.tenant_id):
        assert (
            ProviderCallAttempt.objects.filter(
                call=row, observation="result", http_status=401
            ).count()
            == 2
        )
        assert (
            ProviderReservation.objects.filter(call=row, state="released").count() == 2
        )
        assert IntegrationIssue.objects.filter(
            client_id=spend_link.client_id,
            reason="autenticacao_serpro_recusada",
            owner__isnull=False,
            state="aberta",
        ).exists()


@pytest.mark.parametrize(
    "reply",
    [
        Reply(reset=True),
        Reply(status=401, body=b"complete-length", chunks=(b"x",)),
        Reply(status=500),
        Reply(status=503),
        Reply(status=504),
    ],
    ids=["reset", "partial-401", "500", "503", "504"],
)
def test_ambiguous_response_never_opens_a_reauthentication_attempt(
    spend_link: ClientProviderLink, reply: Reply
) -> None:
    # Given an outcome which does not prove the request was rejected.
    with FakeSerpro((reply, Reply())) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        # When the adapter reports its actual bounded-wire outcome.
        result = perform(ticket, adapter)
        # Then no new permit or automatic claim can resend the effect.
        assert result.state == "incerta"
        assert len(peer.requests) == 1
        assert claim(spend_link.tenant_id, row.pk) is None
    assert reload_call(spend_link, row.pk).attempts == 1


@pytest.mark.parametrize("control", ["pause", "budget"])
def test_retry_rechecks_live_pause_and_budget(
    spend_link: ClientProviderLink, settings: SettingsWrapper, control: str
) -> None:
    # Given controls changed after the complete first response but before retry.
    def change_controls() -> None:
        if control == "pause":
            settings.PROVIDERS_PAUSED = True
        else:
            with tenant_context(spend_link.tenant_id):
                ProviderBudget.objects.update(cap_microbrl=0)

    with FakeSerpro((Reply(status=401), Reply())) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        # When the retry reaches ordinary admission again.
        denial = None
        try:
            perform(ticket, AfterFirstResponse(adapter, change_controls))
        except AdmissionDeniedError as error:
            denial = error
        # Then an earlier claim cannot override the newly closed control.
        assert len(peer.requests) == 1
        assert denial is not None or reload_call(spend_link, row.pk).state in {
            "pausada",
            "bloqueada_cota",
        }
    with tenant_context(spend_link.tenant_id):
        assert (
            ProviderReservation.objects.filter(call=row, state="reserved").count() == 0
        )
        assert ProviderBudget.objects.get().reserved == 0


def test_expired_retry_lease_refuses_before_its_first_byte(
    spend_link: ClientProviderLink, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a genuine retry claim whose new lease expires before dispatch-start.
    original = call_protocol.claim

    def expire_retry(tenant_id: UUID, call_id: UUID) -> Claim | None:
        ticket = original(tenant_id, call_id)
        if ticket is not None:
            with tenant_context(spend_link.tenant_id):
                ProviderCall.objects.filter(pk=ticket.call_id).update(
                    lease_until=timezone.now() - timedelta(seconds=1)
                )
        return ticket

    with FakeSerpro((Reply(status=401), Reply())) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        monkeypatch.setattr(call_protocol, "claim", expire_retry)
        # When the retry is claimed but its lease is no longer usable.
        with pytest.raises(StaleClaimError):
            perform(ticket, adapter)
        # Then the first 401 stays recorded and no second send left the process.
        assert len(peer.requests) == 1
    assert reload_call(spend_link, row.pk).state == "incerta"


def test_timeout_after_401_headers_never_authorizes_reauthentication(
    spend_link: ClientProviderLink, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a real send whose body reader times out after receiving 401 headers.
    def timed_out(
        response: Response, limits: WireLimits, deadline: float
    ) -> ProviderResponse:
        code = "tempo_limite"
        raise TransportFailure(code)

    with FakeSerpro((Reply(status=401), Reply())) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        monkeypatch.setattr(transport_wire, "_consume", timed_out)
        # When the bounded reader reports timeout rather than a complete response.
        result = perform(ticket, adapter)
        # Then status headers alone cannot turn ambiguity into another send.
        assert result.state == "incerta"
        assert len(peer.requests) == 1
        assert claim(spend_link.tenant_id, row.pk) is None


def test_concurrent_claims_of_a_scheduled_retry_admit_one_sender(
    spend_link: ClientProviderLink,
) -> None:
    from apps.integrations.serpro.reauthentication import (  # noqa: PLC0415
        schedule_client_retry,
    )

    # Given a first definitive 401 whose single retry is scheduled, not yet claimed.
    with FakeSerpro((Reply(status=401),)) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        first = call_protocol._perform_once(ticket, adapter)
        assert schedule_client_retry(ticket, first)
    barrier = Barrier(2, timeout=10)

    def contend() -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")
                cursor.execute("SET statement_timeout = '10s'")
            barrier.wait()
            return claim(spend_link.tenant_id, row.pk) is not None
        finally:
            connections.close_all()

    # When two workers race to claim it.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contend), pool.submit(contend)]
        outcomes = [future.result(timeout=15) for future in futures]
    # Then exactly one fresh fence and one new reservation exist for the resend.
    assert sorted(outcomes) == [False, True]
    saved = reload_call(spend_link, row.pk)
    assert (saved.state, saved.fence, saved.attempts) == (CallState.CLAIMED, 2, 1)
    with tenant_context(spend_link.tenant_id):
        assert list(
            ProviderReservation.objects.filter(call=row)
            .order_by("attempt_number")
            .values_list("attempt_number", "state")
        ) == [(1, "released"), (2, "reserved")]


def test_403_procurador_absence_is_billed_and_opens_owned_work(
    spend_link: ClientProviderLink,
) -> None:
    # Given a complete provider denial of the firm's procurador authorization.
    body = json.dumps(
        {
            "status": 403,
            "mensagens": [
                {"codigo": "AcessoNegado-ICGERENCIADOR-016", "texto": "synthetic"}
            ],
            "dados": "{}",
        }
    ).encode()
    with FakeSerpro((Reply(status=403, body=body), Reply())) as peer:
        row, ticket, adapter = client_dispatch(spend_link, peer.origin)
        # When the call finalizes, then denial is not silently treated as free or empty.
        result = perform(ticket, adapter)
        assert result.state == "negada"
        assert result.billed == "sim"
        assert len(peer.requests) == 1
    with tenant_context(spend_link.tenant_id):
        assert ProviderReservation.objects.get(call=row).state == "known"
        assert ProviderBudget.objects.get().known_liability == 240_000
        assert IntegrationIssue.objects.filter(
            client_id=spend_link.client_id,
            reason="autorizacao_procurador_ausente",
            owner__isnull=False,
        ).exists()

"""AXRM-019 repair 1: the immutable start month at send time, and one window per token.

PLAN19-IMMUTABLE-MONTH: a setup plan computed before another setup fenced a different
start month must never send its month to Focus; the check runs against the stored link
under its lock, right before the write. PLAN19-RATE-LIMIT: Focus limits requests per
token, so every alias resolving to the same token shares one 60-request window.
"""

import json
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from django.db import connection as db_connection
from django.db.models import F
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations import transport
from apps.integrations.call_dispatch import Dispatch
from apps.integrations.call_models import CallState
from apps.integrations.focus import adapter as focus_adapter
from apps.integrations.focus.adapter import (
    RATE_LIMITED,
    FocusAdapter,
    company_request,
    limiter_key,
    request_digest,
    token_for,
)
from apps.integrations.focus.company import (
    FocusRefusedError,
    plan_setup,
    run_setup,
    sync_company_status,
)
from apps.integrations.focus.rate_limit import TokenRateLimiter
from apps.integrations.models import ProviderConnection
from apps.integrations.transport_types import ProviderRequest, ProviderResponse
from tests.integrations.test_focus_adapter import COMPANY_ID, World, world

if TYPE_CHECKING:
    from apps.integrations.transport import ProviderTransport

__all__ = ["world"]
db = pytest.mark.django_db(transaction=True)
SHARED = "synthetic-shared-focus-token"
OTHER = "synthetic-other-focus-token"
ALIAS_A = "focus/alias_a"
ALIAS_B = "focus/alias_b"
ALIAS_C = "focus/alias_c"


def _months_sent(world: World) -> list[object]:
    return [
        json.loads(r.body).get("data_inicio_recebimento_nfsen")
        for r in world.fake.requests
        if r.method == "PUT"
    ]


# --- PLAN19-IMMUTABLE-MONTH ---------------------------------------------------------


@db
def test_a_stale_setup_plan_never_sends_a_different_fenced_month(world: World) -> None:
    # Given a November plan computed before a December setup fenced the month.
    stale = plan_setup(world.client.pk, "nfsen_recebidas", "2026-11")
    fresh = plan_setup(world.client.pk, "nfsen_recebidas", "2026-12")
    assert run_setup(fresh).confirmed
    before = len(world.fake.requests)
    # When the stale plan runs (as a real setup and as a dry run).
    for dry_run in (False, True):
        with pytest.raises(FocusRefusedError, match="2026-12"):
            run_setup(stale, dry_run=dry_run)
    # Then nothing more reached Focus and both sides keep December.
    assert len(world.fake.requests) == before
    assert _months_sent(world) == ["2026-12-01"]
    assert world.fake.company["data_inicio_recebimento_nfsen"] == "2026-12-01"
    assert str(world.reload().capture_start_month) == "2026-12-01"


@db
def test_concurrent_setups_never_send_two_start_months(world: World) -> None:
    # Given two plans for different months on an unfenced link, run together.
    plans = [
        plan_setup(world.client.pk, "nfsen_recebidas", month)
        for month in ("2026-11", "2026-12")
    ]
    start = threading.Barrier(len(plans))
    outcomes: list[str] = []
    record = threading.Lock()

    def run(index: int) -> None:
        try:
            start.wait(timeout=10)
            result = "confirmed" if run_setup(plans[index]).confirmed else "unconfirmed"
        except FocusRefusedError:
            result = "refused"
        except Exception as error:  # noqa: BLE001 - report any outcome as data
            result = type(error).__name__
        finally:
            db_connection.close()
        with record:
            outcomes.append(result)

    threads = [
        threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(plans))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not [thread for thread in threads if thread.is_alive()]
    # Then exactly one month wins and no request ever carried the other one.
    assert sorted(outcomes) == ["confirmed", "refused"]
    stored = str(world.reload().capture_start_month)
    assert set(_months_sent(world)) == {stored}


# --- PLAN19-RATE-LIMIT ----------------------------------------------------------------


def _aliases(monkeypatch: pytest.MonkeyPatch) -> tuple[ProviderConnection, ...]:
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_ALIAS_A", SHARED)
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_ALIAS_B", SHARED)
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_ALIAS_C", OTHER)
    return tuple(
        ProviderConnection(environment="homologacao", secret_ref=alias)
        for alias in (ALIAS_A, ALIAS_B, ALIAS_C)
    )


def test_the_window_key_is_the_effective_token_never_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, third = _aliases(monkeypatch)
    assert token_for(first) == token_for(second)
    # One token, one key; another token, another key.
    assert limiter_key(first) == limiter_key(second)
    assert limiter_key(first) != limiter_key(third)
    # The key never carries the token value or a reversible form of it.
    for key in (limiter_key(first), limiter_key(third)):
        assert SHARED not in key
        assert OTHER not in key
    assert limiter_key(ProviderConnection(environment="fake", secret_ref="")) != (
        limiter_key(first)
    )


@dataclass
class _Transport:
    sent: int = 0

    def send(self, operation: str, request: ProviderRequest) -> ProviderResponse:
        self.sent += 1
        body = json.dumps({"id": COMPANY_ID, "cnpj": "11222333000181"}).encode()
        return ProviderResponse(200, body)


def _dispatch(request: ProviderRequest) -> Dispatch:
    receipt = SimpleNamespace(
        request_digest=request_digest(request),
        operation="FC-EMP",
        operation_class="configuration",
    )
    return cast("Dispatch", SimpleNamespace(receipt=receipt))


def test_aliases_of_one_token_share_one_sixty_request_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given one Focus token reachable through two secret aliases.
    first, second, third = _aliases(monkeypatch)
    clock = lambda: 1000.0  # noqa: E731 - a frozen minute
    monkeypatch.setattr(focus_adapter, "LIMITER", TokenRateLimiter(clock=clock))
    wire = _Transport()
    request = company_request(str(COMPANY_ID))

    def adapter(connection: ProviderConnection) -> FocusAdapter:
        return FocusAdapter(
            cast("ProviderTransport", wire),
            request,
            token=token_for(connection),
            limiter_key=limiter_key(connection),
        )

    # When 60 requests go out through the first alias inside one minute.
    for _ in range(60):
        assert adapter(first).send(_dispatch(request)).state == CallState.SUCCESS
    # Then the 61st, through the second alias, is refused before the wire.
    refused = adapter(second).send(_dispatch(request))
    assert (refused.state, refused.provider_code) == (CallState.FAILED, RATE_LIMITED)
    assert wire.sent == 60
    # And another token keeps its own window.
    assert adapter(third).send(_dispatch(request)).state == CallState.SUCCESS
    assert wire.sent == 61


@db
def test_changing_a_connections_alias_keeps_the_same_tokens_window(
    world: World, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    # Given the real status-sync path in homologation, rerouted to the loopback fake,
    # and a frozen minute so the outcome never depends on how fast the host is.
    monkeypatch.setitem(transport.ORIGINS, ("focus", "homologacao"), world.fake.origin)
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_ALIAS_A", SHARED)
    monkeypatch.setenv("FOCUS_SECRET_FOCUS_ALIAS_B", SHARED)
    monkeypatch.setattr(
        focus_adapter, "LIMITER", TokenRateLimiter(clock=lambda: 1000.0)
    )
    settings.FOCUS_ENVIRONMENT = "homologacao"
    with tenant_context(world.tenant.pk):
        ProviderConnection.objects.filter(pk=world.link.connection_id).update(
            environment="homologacao", secret_ref=ALIAS_A, version=F("version") + 1
        )
    for _ in range(60):
        assert sync_company_status(world.link).state == CallState.SUCCESS
    # When the operator re-points the connection at another alias of the same token.
    with tenant_context(world.tenant.pk):
        ProviderConnection.objects.filter(pk=world.link.connection_id).update(
            secret_ref=ALIAS_B, version=F("version") + 1
        )
    outcome = sync_company_status(world.link)
    # Then the 61st request in that minute is still refused, unsent.
    assert (outcome.state, outcome.provider_code) == (CallState.FAILED, RATE_LIMITED)
    assert len(world.fake.requests) == 60

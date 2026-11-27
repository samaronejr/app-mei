"""AXRM-045 caller x cut matrix for the claim contract (re-scoped core-a-moved).

Each node drives one caller through one cut on the real protocol, asserts the
invariants I1-I4 and records its outcome tuple with `record_property`, so the
same file run on two heads can be compared node by node. Changed cells also
assert the end this part chooses for a moved version; parity cells record what
the caller does and are compared, not judged.
"""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

import pytest
from django.db import connection, connections, transaction
from django.db.models import F
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.integrations.serpro import payments
from apps.integrations.serpro.models import PaymentQuery
from apps.obligations.models import FederalConsultation, FederalPayment
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations import sitfis_support
from tests.integrations.pagtoweb_support import World, item, page, receipt_body, world
from tests.integrations.pgmei_support import GuideWorld, das_reply, guide_world
from tests.integrations.sitfis_support import clock, processing_202, protocol_reply
from tests.integrations.test_pagtoweb_ownership import lease_runs_out

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "guide_world", "world"]

# The SITFIS world, under its own fixture name beside the PAGTOWEB one.
sitfis_world = pytest.fixture(name="sitfis_world")(
    cast("Any", sitfis_support.world).__wrapped__
)

LIVE: frozenset[str] = frozenset(
    {CallState.RESERVED, CallState.CLAIMED, CallState.STARTED, CallState.RETRY}
)
CHANGED: tuple[str, ...] = (
    "moved-before-claim",
    "moved-before-retry-claim",
    "moved-then-redelivery",
    "moved-call-locked",
)
PARITY: tuple[str, ...] = (
    # Moved after a claim (dispatch records it) or with a pause (the pause wins):
    # the same on the base, so these prove no regression, not a change.
    "moved-after-claim",
    "moved-after-retry-claim",
    "moved-paused-claim",
    "moved-paused-dispatch",
    "pause-claim",
    "pause-dispatch",
    "pause-retry-claim",
    "pause-retry-dispatch",
    "unpause-after-pause-claim",
    "unpause-after-pause-dispatch",
    "unpause-after-pause-retry-claim",
    "unpause-after-pause-retry-dispatch",
    "authority-change",
    "lost-link",
    "lease-expiry",
    "quota",
)


@dataclass
class Caller:
    """One caller of the claim contract, driven through the real protocol."""

    module: ModuleType
    tenant_id: UUID
    link_id: UUID
    operation: str
    settings: SettingsWrapper
    actor: User | None
    run: Callable[[], object]
    redeliver: Callable[[], object]
    sends: Callable[[], int]
    aggregate: Callable[[], object]
    script_ok: Callable[[], None]
    script_401: Callable[[], None]
    authority_service: str = ""
    outcomes: list[str] = field(default_factory=list)


def _outcome(action: Callable[[], object]) -> str:
    try:
        result = action()
    except SystemExit:
        return "died"
    except Exception as error:  # noqa: BLE001 - the outcome is what is recorded
        return f"raised {type(error).__name__}: {error}"
    state = getattr(result, "state", None) or getattr(result, "code", None) or result
    return f"returned {state}"


def _move(caller: Caller) -> None:
    with tenant_context(caller.tenant_id):
        connection_id = ClientProviderLink.objects.get(pk=caller.link_id).connection_id
        ProviderConnection.objects.filter(pk=connection_id).update(
            version=F("version") + 1
        )


def _pause(caller: Caller) -> None:
    caller.settings.SERPRO_PAUSED = True


def _calls(caller: Caller) -> list[ProviderCall]:
    with tenant_context(caller.tenant_id):
        return list(
            ProviderCall.objects.filter(operation=caller.operation).order_by(
                "created_at", "pk"
            )
        )


@contextmanager
def _call_locked(caller: Caller) -> Iterator[None]:
    """Another connection holds the caller's call rows until the block ends."""
    holding, done = threading.Event(), threading.Event()
    keys = [call.pk for call in _calls(caller)]

    def hold() -> None:
        try:
            with tenant_context(caller.tenant_id), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '10s'")
                list(ProviderCall.objects.select_for_update().filter(pk__in=keys))
                holding.set()
                assert done.wait(timeout=30)
        finally:
            connections.close_all()

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=15)
    try:
        yield
    finally:
        done.set()
        holder.join(timeout=30)


def _wrap(
    target: ModuleType,
    before: Callable[[], None] | None,
    after: Callable[[], None] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = target.claim
    fired: list[bool] = []

    def claim_with_cut(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if fired:
            return real(*args, **kwargs)
        fired.append(True)
        if before is not None:
            before()
        ticket = real(*args, **kwargs)
        if after is not None:
            after()
        return ticket

    monkeypatch.setattr(target, "claim", claim_with_cut)


def _apply(caller: Caller, cut: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: C901, PLR0912 - one branch per cut
    protocol = import_module("apps.integrations.call_protocol")

    def move() -> None:
        _move(caller)

    def pause() -> None:
        _pause(caller)

    retry = "retry" in cut
    if retry:
        caller.script_401()
    caller.script_ok()

    def die() -> None:
        raise SystemExit

    def both() -> None:
        move()
        pause()

    base = cut.removeprefix("unpause-after-")
    if base in {"moved-before-claim", "moved-then-redelivery"}:
        _wrap(caller.module, move, None, monkeypatch)
    elif base == "moved-after-claim":
        _wrap(caller.module, None, move, monkeypatch)
    elif base == "moved-before-retry-claim":
        _wrap(protocol, move, None, monkeypatch)
    elif base == "moved-after-retry-claim":
        _wrap(protocol, None, move, monkeypatch)
    elif base == "moved-paused-claim":
        _wrap(caller.module, both, None, monkeypatch)
    elif base == "moved-paused-dispatch":
        _wrap(caller.module, None, both, monkeypatch)
    elif base == "pause-claim":
        _wrap(caller.module, pause, None, monkeypatch)
    elif base == "pause-dispatch":
        _wrap(caller.module, None, pause, monkeypatch)
    elif base == "pause-retry-claim":
        _wrap(protocol, pause, None, monkeypatch)
    elif base == "pause-retry-dispatch":
        _wrap(protocol, None, pause, monkeypatch)
    elif base == "lease-expiry":
        _wrap(caller.module, None, die, monkeypatch)
    elif base == "authority-change":
        _wrap(caller.module, lambda: _revoke(caller), None, monkeypatch)
    elif base == "lost-link":
        _wrap(caller.module, lambda: _lose_link(caller), None, monkeypatch)
    elif base == "quota":
        with tenant_context(caller.tenant_id):
            ProviderBudget.objects.update(cap_microbrl=0)


def _revoke(caller: Caller) -> None:
    with tenant_context(caller.tenant_id):
        link = ClientProviderLink.objects.select_related("client").get(
            pk=caller.link_id
        )
        head = head_record(link.client_id, caller.authority_service)
        assert head is not None
        assert caller.actor is not None
        revoke_authority(
            caller.actor,
            link.client_id,
            service=caller.authority_service,
            expected_version=head.version,
        )


def _lose_link(caller: Caller) -> None:
    with tenant_context(caller.tenant_id):
        ClientProviderLink.objects.filter(pk=caller.link_id).update(status="revogada")


def _drive(caller: Caller, cut: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _apply(caller, cut, monkeypatch)
    if cut == "moved-call-locked":
        # The version moves, and another runner holds the call through the
        # first claim only; a later delivery then settles it.
        with ExitStack() as held:

            def move_and_lock() -> None:
                _move(caller)
                held.enter_context(_call_locked(caller))

            _wrap(caller.module, move_and_lock, held.close, monkeypatch)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
            try:
                caller.outcomes.append(_outcome(caller.run))
            finally:
                held.close()
                with connection.cursor() as cursor:
                    cursor.execute("RESET lock_timeout")
        caller.outcomes.append(_outcome(caller.redeliver))
        return
    caller.outcomes.append(_outcome(caller.run))
    if cut.startswith("unpause-after-"):
        caller.settings.SERPRO_PAUSED = False
        caller.outcomes.append(_outcome(caller.redeliver))
    if cut in {"moved-then-redelivery", "lease-expiry"}:
        if cut == "lease-expiry":
            with tenant_context(caller.tenant_id):
                ProviderCall.objects.filter(operation=caller.operation).update(
                    lease_until=timezone.now() - timedelta(seconds=1)
                )
        caller.outcomes.append(_outcome(caller.redeliver))


def _facts(caller: Caller) -> dict[str, object]:
    calls = _calls(caller)
    with tenant_context(caller.tenant_id):
        slots = sorted(
            ProviderReservation.objects.filter(call__in=calls).values_list(
                "call__request_key", "attempt_number", "state"
            )
        )
        budget = list(ProviderBudget.objects.values_list("reserved", "known_liability"))
        owned = sorted(IntegrationIssue.objects.values_list("reason", flat=True))
    return {
        "calls": [(call.state, call.provider_code, call.attempts) for call in calls],
        "keys": [call.request_key for call in calls],
        "slots": [(attempt, state) for _key, attempt, state in slots],
        "slot_keys": [(key, state) for key, _attempt, state in slots],
        "budget": budget,
        "sends": caller.sends(),
        "aggregate": caller.aggregate(),
        "owned": owned,
        "outcomes": list(caller.outcomes),
    }


def _check(
    caller: Caller, cut: str, record_property: Callable[[str, object], None]
) -> dict[str, object]:
    facts = _facts(caller)
    record_property("tuple", json.dumps(facts, default=str, sort_keys=True))
    keys = facts["keys"]
    assert isinstance(keys, list)
    # I1: one call per request key.
    assert len(keys) == len(set(keys))
    # I2: every send belongs to an attempt the protocol started.
    calls = facts["calls"]
    assert isinstance(calls, list)
    sends = facts["sends"]
    assert isinstance(sends, int)
    assert sends <= sum(attempts for _state, _code, attempts in calls)
    # I3: no reservation stays `reserved` on a terminal call.
    states = {call.request_key: call.state for call in _calls(caller)}
    held = facts["slot_keys"]
    assert isinstance(held, list)
    assert [
        k for k, state in held if state == "reserved" and states[k] not in LIVE
    ] == []
    # I4: a final call is never rewritten by a later delivery of the consumer.
    final = [
        (c.pk, c.state, c.provider_code) for c in _calls(caller) if c.state not in LIVE
    ]
    caller.settings.SERPRO_PAUSED = False
    _outcome(caller.redeliver)
    again = {c.pk: (c.state, c.provider_code) for c in _calls(caller)}
    assert [
        (pk, state, code) for pk, state, code in final if again[pk] != (state, code)
    ] == []
    if cut in CHANGED:
        # This part's choice: a moved version is a recorded, terminal refusal,
        # never left reserved for a later claim.
        assert any(state == CallState.AUTHORITY for state, _c, _a in calls) or any(
            state == CallState.PAUSED for state, _c, _a in calls
        )
        assert not any(state == CallState.RESERVED for state, _c, _a in calls)
    return facts


def _page_caller(world: World, settings: SettingsWrapper) -> Caller:
    queries: list[PaymentQuery] = []

    def run() -> object:
        query = world.consult()
        queries.append(query)
        return query

    def redeliver() -> object:
        with tenant_context(world.termo.tenant.pk):
            query = PaymentQuery.objects.order_by("-created_at").first()
        if query is None or query.state not in {"na_fila", "em_andamento"}:
            return query
        if query.settlement_owner is not None:
            lease_runs_out(world, query.pk)
        return payments.run_payment_query(query.tenant_id, query.pk)

    def aggregate() -> object:
        with tenant_context(world.termo.tenant.pk):
            return list(PaymentQuery.objects.values_list("state", "failure_code"))

    return Caller(
        module=payments,
        tenant_id=world.termo.tenant.pk,
        link_id=world.link.pk,
        operation="SP-PAG",
        settings=settings,
        actor=world.termo.actor,
        run=run,
        redeliver=redeliver,
        sends=lambda: len(world.sent()),
        aggregate=aggregate,
        script_ok=lambda: world.script(page([item("DAS000000000045")])),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="pagtoweb",
    )


def _receipt_caller(
    world: World, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> Caller:
    world.script(page([item("DAS000000000045")]))
    world.consult()
    with tenant_context(world.termo.tenant.pk):
        das = FederalPayment.objects.get()
    tasks = import_module("apps.integrations.serpro.tasks")
    later: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        tasks.emit_payment_receipt_task,
        "apply_async",
        lambda args, **_kwargs: later.append(args),
    )
    args = (str(world.termo.tenant.pk), str(das.pk), str(world.termo.actor.pk))
    pdf = import_module("tests.integrations.test_pagtoweb").PDF
    issued = json.dumps({"pdf": __import__("base64").b64encode(pdf).decode()})
    return Caller(
        module=payments,
        tenant_id=world.termo.tenant.pk,
        link_id=world.link.pk,
        operation="SP-COMP",
        settings=settings,
        actor=world.termo.actor,
        run=lambda: tasks.emit_payment_receipt_task(*args),
        redeliver=lambda: tasks.emit_payment_receipt_task(*args),
        sends=lambda: len(world.sent("/Emitir")),
        aggregate=lambda: len(later),
        script_ok=lambda: world.script(Reply(body=receipt_body(issued))),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="pagtoweb",
    )


# PAGTOWEB settles a page whose claim another runner's lock skipped (base
# behaviour); its moved-call-locked cell is owned by 45-call-lifecycle.
@pytest.mark.parametrize(
    "cut", [*(cut for cut in CHANGED if cut != "moved-call-locked"), *PARITY]
)
def test_page(
    world: World,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    cut: str,
) -> None:
    caller = _page_caller(world, settings)
    _drive(caller, cut, monkeypatch)
    _check(caller, cut, record_property)


@pytest.mark.parametrize("cut", [*CHANGED, *PARITY, "actor-lost"])
def test_receipt(
    world: World,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    cut: str,
) -> None:
    caller = _receipt_caller(world, settings, monkeypatch)
    if cut == "actor-lost":
        from apps.tenants.models import Membership, TenantRole  # noqa: PLC0415

        Membership.objects.filter(user=world.termo.actor).update(
            role=TenantRole.STAFF_ACCOUNTANT
        )
        caller.script_ok()
        caller.outcomes.append(_outcome(caller.run))
    else:
        _drive(caller, cut, monkeypatch)
    _check(caller, cut, record_property)


def _guide_caller(
    world: GuideWorld, settings: SettingsWrapper, peer: FakeSerpro
) -> Caller:
    pgmei = import_module("apps.integrations.serpro.pgmei")

    def run() -> object:
        return pgmei.obtain_das_guide(
            world.owner, world.tenant_id, world.cycle.pk, origin=peer.origin
        )

    return Caller(
        module=pgmei,
        tenant_id=world.tenant_id,
        link_id=world.link.pk,
        operation="SP-DAS",
        settings=settings,
        actor=world.owner,
        run=run,
        redeliver=run,
        sends=lambda: len(peer.requests),
        aggregate=lambda: None,
        script_ok=lambda: peer.replies.append(das_reply()),
        script_401=lambda: peer.replies.append(Reply(status=401)),
    )


@pytest.mark.parametrize(
    "cut", [*CHANGED, *(cut for cut in PARITY if cut != "authority-change")]
)
def test_guide(
    guide_world: GuideWorld,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    cut: str,
) -> None:
    with FakeSerpro(()) as peer:
        caller = _guide_caller(guide_world, settings, peer)
        _drive(caller, cut, monkeypatch)
        _check(caller, cut, record_property)


def _sitfis_caller(world: Any, settings: SettingsWrapper) -> Caller:  # noqa: ANN401
    mod = import_module("apps.integrations.serpro.sitfis")
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    step = mod._decide(world.tenant_id, report.pk)
    assert step is not None

    def run() -> object:
        mod._run(world.tenant_id, report.pk, step)
        return world.reload(report)

    def aggregate() -> object:
        current = world.reload(report)
        with tenant_context(world.tenant_id):
            receipt = FederalConsultation.objects.get(pk=current.consultation_id)
        return (current.state, receipt.provider_ref != "")

    return Caller(
        module=mod,
        tenant_id=world.tenant_id,
        link_id=step.link_id,
        operation=step.operation,
        settings=settings,
        actor=world.termo.actor,
        run=run,
        redeliver=lambda: world.advance(world.reload(report)),
        sends=lambda: len(world.sent("/Emitir")),
        aggregate=aggregate,
        script_ok=lambda: world.script(processing_202(0)),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="sitfis",
    )


@pytest.mark.parametrize("cut", [*CHANGED, *PARITY, "scanner-off"])
def test_sitfis(
    sitfis_world: Any,  # noqa: ANN401
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    cut: str,
) -> None:
    caller = _sitfis_caller(sitfis_world, settings)
    if cut == "scanner-off":
        settings.CLAMAV_ENABLED = False
        caller.script_ok()
        caller.outcomes.append(_outcome(caller.run))
    else:
        _drive(caller, cut, monkeypatch)
    _check(caller, cut, record_property)

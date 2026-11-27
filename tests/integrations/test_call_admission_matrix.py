"""AXRM-045 caller x cut matrix for the claim contract (re-scoped core-a-moved).

This part claims only what its edits change:

- MOVED, red on 1bd599f: a version move at the first or the retry claim records
  the target call, bound before the cut, as `bloqueada_autoridade` with the cause
  frozen at refusal, releases its unsent slot, sends nothing more, and the caller
  hands off exactly (oracles M1-M5 in `_moved`).
- TRANSIENT, red on 85ca1f5 (the 3440c9a tree) by design, because that tree
  recorded every claim refusal: a pause, an ordinary authority veto, a disabled
  link or a lapsed termo at claim, without a version move, leaves no call
  `bloqueada_autoridade` or `pausada` (oracle T1).
- Boundary pins, holding on every tree: a move after the claim ends the target
  `bloqueada_autoridade` with its slots released; a pause and a move before
  dispatch end it `pausada`, because the pause guard runs first
  (`call_state.py:57-68` before `:69-81`).
- Every other node is recorded for comparison across trees, never judged.

Not claimed: I1-I4 beyond the moved refusal (fencing at send, one reservation per
attempt and terminal immutability are 45-call-lifecycle's acceptance), any lock
order or deadlock freedom, settlement-lock coverage outside SITFIS, the scanner
veto at `_step`, access loss between deliveries, link or termo loss at a later
preflight (all 45-call-lifecycle), and `page[moved-claim-skips-held-call]`, the
known PAGTOWEB skipped-claim defect (`payments.py:453-460,819-832`) owned there.

Each node binds its target `(tenant, client, operation, request key, call,
effect key)` when the caller first claims, snapshots once after the cut and once
after an unpaused settle pass (the caller's real delivery of the same intent; a
guide click is a new intent only past a refusal that sent nothing,
`pgmei.py:418-433`), and records both under aliases as its `tuple`.

Cuts per caller (page, receipt, guide, SITFIS): x exercised here, o exercised
outside the matrix, - not applicable, d deferred to 45-call-lifecycle.

    cut                                  class      P R G S  note
    moved-before-claim                   MOVED      x x x x
    moved-before-retry-claim             MOVED      x x x x  after an unbilled 401
    moved-then-redelivery                MOVED      x x x x  a redelivery repeats it
    moved-claim-skips-held-call          MOVED      d x x x  (1)
    pause-claim, pause-retry-claim       TRANSIENT  x x x x
    unpause-after-pause-claim            TRANSIENT  x x x x
    unpause-after-pause-retry-claim      TRANSIENT  x x x x
    moved-paused-claim                   TRANSIENT  x x x x  the pause guard first
    authority-revoked-at-claim           TRANSIENT  x x - x  (2)
    link-disabled-at-claim               TRANSIENT  x x x x  a pause, as pause-claim
    termo-lost-at-claim                  TRANSIENT  x x x x  no version move
    moved-after-claim                    pin        x x x x  dispatch records it
    moved-after-retry-claim              pin        x x x x  dispatch records it
    moved-paused-dispatch                pin        x x x x  ends pausada
    pause-dispatch, pause-retry-dispatch recorded   x x x x
    unpause-after-pause-*dispatch        recorded   x x x x
    lease-expiry, quota                  recorded   x x x x
    actor-lost-before-call               recorded   - x - -  (3)
    actor-lost-between-deliveries        recorded   - x - -  (3), characterization
    scanner-off-after-prepare            recorded   - - - x  (4)
    scanner-off-before-decision          recorded   - - - x  (4), characterization
    locked at settlement                 -          - - d o  (5)
    budget error settling a sent receipt -          - o - -  (6)

(1) Page: the known defect. The guide's holder ends when its claim returns,
    because its settle locks the call; the others hold through the first pass.
(2) PGMEI needs no client procuracao (`integrations/models.py:324-329`).
(3) The page has no initiating actor here, each guide click checks its own
    actor, SITFIS has none; recovery after access loss is 45-call-lifecycle's.
(4) Only SITFIS has a scanner gate; the `_step` veto is 45-call-lifecycle's.
(5) A call or connection locked at settlement: SITFIS by
    `test_sitfis_admission.py:571,587` (mutants OS, SA); page and receipt read
    the refused call unlocked (`payments.py:609-611,1049-1050`).
(6) `test_serpro_denials.py:1307-1344` (mutant BC) keeps it propagating.

A recorded node may record a bad inherited result without certifying it.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import import_module
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from django.db import connection, connections, transaction
from django.db.models import F
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceLink
from apps.cycles.models import Exception as CycleException
from apps.integrations.authority import head_record, revoke_authority
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState, ProviderCall, ProviderCallAttempt
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.integrations.serpro import payments
from apps.integrations.serpro.models import FirmIntegrationIssue, PaymentQuery
from apps.obligations.models import Document, FederalConsultation, FederalPayment
from apps.tenants.models import Membership, TenantRole
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations import sitfis_support
from tests.integrations.pagtoweb_support import World, item, page, receipt_body, world
from tests.integrations.pgmei_support import GuideWorld, das_reply, guide_world
from tests.integrations.sitfis_support import clock, processing_202, protocol_reply
from tests.integrations.test_pagtoweb_ownership import lease_runs_out

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from apps.accounts.models import User

DB = pytest.mark.django_db(transaction=True)
__all__ = ["clock", "guide_world", "world"]

# The SITFIS world, under its own fixture name beside the PAGTOWEB one.
sitfis_world = pytest.fixture(name="sitfis_world")(
    cast("Any", sitfis_support.world).__wrapped__
)

MOVED, TRANSIENT, RECORDED = "moved", "transient", "recorded"
PIN_AUTHORITY, PIN_PAUSED = "pin-authority", "pin-paused"
SKIP: Final = "moved-claim-skips-held-call"
CUTS: Final[dict[str, str]] = {
    "moved-before-claim": MOVED,
    "moved-before-retry-claim": MOVED,
    "moved-then-redelivery": MOVED,
    SKIP: MOVED,
    "pause-claim": TRANSIENT,
    "pause-retry-claim": TRANSIENT,
    "unpause-after-pause-claim": TRANSIENT,
    "unpause-after-pause-retry-claim": TRANSIENT,
    "moved-paused-claim": TRANSIENT,
    "authority-revoked-at-claim": TRANSIENT,
    "link-disabled-at-claim": TRANSIENT,
    "termo-lost-at-claim": TRANSIENT,
    "moved-after-claim": PIN_AUTHORITY,
    "moved-after-retry-claim": PIN_AUTHORITY,
    "moved-paused-dispatch": PIN_PAUSED,
    "pause-dispatch": RECORDED,
    "pause-retry-dispatch": RECORDED,
    "unpause-after-pause-dispatch": RECORDED,
    "unpause-after-pause-retry-dispatch": RECORDED,
    "lease-expiry": RECORDED,
    "quota": RECORDED,
    "actor-lost-before-call": RECORDED,
    "actor-lost-between-deliveries": RECORDED,
    "scanner-off-after-prepare": RECORDED,
    "scanner-off-before-decision": RECORDED,
}
ACTOR_CUTS: Final = ("actor-lost-before-call", "actor-lost-between-deliveries")
SCANNER_CUTS: Final = ("scanner-off-after-prepare", "scanner-off-before-decision")
COMMON: Final = [cut for cut in CUTS if cut not in {*ACTOR_CUTS, *SCANNER_CUTS}]
LIVE: Final = frozenset(
    {CallState.RESERVED, CallState.CLAIMED, CallState.STARTED, CallState.RETRY}
)
# What each caller returns or raises once a moved claim is refused, and what its
# first pass returns while another runner holds the call through the claim.
REFUSED: Final = {
    "page": "returned falha",
    "receipt": "returned bloqueada_autoridade",
    "guide": "raised GuideRefused: sem_autorizacao",
    "sitfis": "returned falha",
}
SKIPPED: Final = {
    "receipt": "returned reservada",
    "guide": "returned em_andamento",
    "sitfis": "returned emitindo",
}
# The target's own record of the definitive 401 that precedes a retry claim.
UNBILLED_401: Final = ["target", 1, "result", 401, "nao"]


@dataclass
class Caller:
    """One caller of the claim contract, driven through its real public path."""

    name: str
    module: ModuleType
    tenant_id: UUID
    link_id: UUID
    operation: str
    settings: SettingsWrapper
    actor: User | None
    gateway: FakeSerpro
    run: Callable[[], object]
    redeliver: Callable[[], object]
    sends: Callable[[], int]
    aggregate: Callable[[dict[str, Any]], dict[str, object]]
    named: Callable[[], dict[str, str]]
    script_ok: Callable[[], None]
    script_401: Callable[[], None]
    authority_service: str = ""
    ordinal: Callable[[ProviderCall], tuple[object, ...]] = lambda call: (
        call.created_at,
        str(call.pk),
    )
    target: dict[str, object] | None = None
    retried: list[UUID] = field(default_factory=list)
    passes: list[list[str]] = field(default_factory=list)
    performs: list[str] = field(default_factory=list)
    queued: list[tuple[str, tuple[object, ...], bool]] = field(default_factory=list)
    unscripted: list[str] = field(default_factory=list)
    skip: dict[str, object] | None = None
    baseline: dict[str, Any] = field(default_factory=dict)


def _outcome(action: Callable[[], object]) -> str:
    try:
        result = action()
    except SystemExit:
        return "died"
    except Exception as error:  # noqa: BLE001 - the outcome is what is recorded
        return f"raised {type(error).__name__}: {error}"
    state = getattr(result, "state", None) or getattr(result, "code", None) or result
    return f"returned {state}"


def _budget() -> list[int]:
    rows = ProviderBudget.objects.values_list(
        "reserved", "known_liability", "uncertain_exposure"
    )
    return [sum(row[index] for row in rows) for index in range(3)]


OWNED_MODELS: Final = (
    ("issue", IntegrationIssue, "reason", "evidence_refs"),
    ("exception", CycleException, "reason_code", "subject_ref"),
    ("firm", FirmIntegrationIssue, "reason", "evidence_refs"),
)


def _observe(caller: Caller, monkeypatch: pytest.MonkeyPatch) -> None:
    """Count unscripted sends and queued continuations; take the node's baseline."""
    gateway = caller.gateway
    real_reply = gateway.reply_for

    def counted(request: Any) -> Reply:  # noqa: ANN401 - the fake's own type
        if not gateway.replies:
            caller.unscripted.append(request.path)
        return real_reply(request)

    monkeypatch.setattr(gateway, "reply_for", counted)
    tasks = import_module("apps.integrations.serpro.tasks")
    sitfis_tasks = import_module("apps.integrations.serpro.sitfis_tasks")
    for task in (
        tasks.run_payment_query_task,
        tasks.emit_payment_receipt_task,
        sitfis_tasks.advance_situation_report_task,
    ):
        name = task.name.rsplit(".", 1)[-1]

        def queue(
            args: tuple[object, ...] = (),
            _kwargs: object = None,
            *,
            name: str = name,
            **options: object,
        ) -> None:
            timed = "eta" in options or "countdown" in options
            caller.queued.append((name, tuple(args), timed))

        monkeypatch.setattr(task, "apply_async", queue)
    with tenant_context(caller.tenant_id):
        caller.baseline = {
            "budget": _budget(),
            "sends": caller.sends(),
            "consultations": set(
                FederalConsultation.objects.values_list("pk", flat=True)
            ),
            **{
                kind: dict(model.objects.values_list("pk", "version"))
                for kind, model, *_ in OWNED_MODELS
            },
        }


def _owned(baseline: dict[str, Any]) -> list[tuple[str, str, str, list[object]]]:
    """Owned work opened or changed during the node, with what it is bound to."""
    rows = []
    for kind, model, reason, refs in OWNED_MODELS:
        for pk, version, why, state, bound in model.objects.values_list(
            "pk", "version", reason, "state", refs
        ):
            if baseline[kind].get(pk) != version:
                rows.append(
                    (kind, why, state, bound if isinstance(bound, list) else [bound])
                )
    return sorted(rows, key=lambda row: (row[0], row[1], row[2], len(row[3])))


def _snapshot(caller: Caller) -> dict[str, Any]:
    """Everything a node compares, as raw rows; `_Aliases` names the identities."""
    with tenant_context(caller.tenant_id):
        calls = sorted(
            ProviderCall.objects.filter(operation=caller.operation),
            key=caller.ordinal,
        )
        ids = [call.pk for call in calls]
        attempts = list(
            ProviderCallAttempt.objects.filter(call_id__in=ids).values_list(
                "call_id", "attempt_number", "observation", "http_status", "billed"
            )
        )
        slots = list(
            ProviderReservation.objects.filter(call_id__in=ids).values_list(
                "call_id", "attempt_number", "state"
            )
        )
        budget = _budget()
        owned = _owned(caller.baseline)
        aggregate = caller.aggregate(caller.baseline)
    return {
        "calls": calls,
        "attempts": attempts,
        "slots": slots,
        "budget": [
            now - before
            for now, before in zip(budget, caller.baseline["budget"], strict=True)
        ],
        "sends": caller.sends() - caller.baseline["sends"],
        "unscripted": len(caller.unscripted),
        "aggregate": aggregate,
        "owned": owned,
        "queued": list(caller.queued),
    }


class _Aliases:
    """Name every within-run identity by its role or by its first appearance."""

    def __init__(self, caller: Caller, snapshots: list[dict[str, Any]]) -> None:
        self.caller = caller
        self.counts: dict[str, int] = {}
        with tenant_context(caller.tenant_id):
            client_id = ClientProviderLink.objects.get(pk=caller.link_id).client_id
            budgets = list(ProviderBudget.objects.values_list("pk", flat=True))
        self.names: dict[str, str] = {
            str(caller.tenant_id): "tenant",
            str(client_id): "client",
            str(caller.link_id): "link",
            **{str(pk): f"budget#{n}" for n, pk in enumerate(budgets, 1)},
            **caller.named(),
        }
        if caller.actor is not None:
            self.names[str(caller.actor.pk)] = "actor"
        bound = caller.target["call"] if caller.target is not None else None
        if bound is not None:
            self.names[str(bound)] = "target"
        seen: list[str] = []
        for snapshot in snapshots:
            seen += [
                str(call.pk) for call in snapshot["calls"] if str(call.pk) not in seen
            ]
        others = [raw for raw in seen if raw != str(bound)]
        for n, raw in enumerate(others, 1):
            if bound is None:
                self.names[raw] = f"call#{n}"
            else:
                self.names[raw] = "successor" if n == 1 else f"successor#{n}"

    def of(self, value: object, kind: str = "ref") -> object:
        if value is None or value == "":
            return value
        raw = str(value)
        if raw not in self.names:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.names[raw] = f"{kind}#{self.counts[kind]}"
        return self.names[raw]

    def call(self, call: ProviderCall) -> dict[str, object]:
        return {
            "id": self.of(call.pk),
            "state": call.state,
            "code": call.provider_code,
            "attempts": call.attempts,
            "billed": call.billed,
            "settled": call.settled_microbrl,
            "version": call.version,
            "fence": call.fence,
            "owner": self.of(call.lease_owner, "owner"),
            "lease": self.of(call.lease_until, "time"),
            "started": self.of(call.dispatch_started_at, "time"),
            "finalized": self.of(call.finalized_at, "time"),
            "retry_of": self.of(call.retry_of_id),
        }

    def aggregate(self, raw: dict[str, object]) -> dict[str, object]:
        ref = raw["ref"]
        named = (
            [[outcome, self.of(value)] for outcome, value in ref]
            if isinstance(ref, list)
            else self.of(ref)
        )
        return {**raw, "ref": named}

    def snapshot(self, raw: dict[str, Any]) -> dict[str, object]:
        calls: list[ProviderCall] = raw["calls"]
        target = self.caller.target
        binding = None
        if target is not None:
            row = next((call for call in calls if call.pk == target["call"]), None)
            binding = (
                row is not None
                and {
                    "tenant": row.tenant_id,
                    "client": row.client_id,
                    "operation": row.operation,
                    "request_key": row.request_key,
                    "call": row.pk,
                    "effect": row.business_effect_key,
                }
                == target
            )
        terminal = {call.pk for call in calls if call.state not in LIVE}

        def ordered(rows: list[list[object]]) -> list[list[object]]:
            return sorted(rows, key=lambda row: json.dumps(row, default=str))

        return {
            "calls": [self.call(call) for call in calls],
            "attempts": ordered(
                [[self.of(row[0]), *row[1:]] for row in raw["attempts"]]
            ),
            "slots": ordered([[self.of(row[0]), *row[1:]] for row in raw["slots"]]),
            "budget": raw["budget"],
            "sends": raw["sends"],
            "unscripted": raw["unscripted"],
            "aggregate": self.aggregate(raw["aggregate"]),
            "owned": ordered(
                [
                    [kind, why, state, sorted(str(self.of(ref)) for ref in refs)]
                    for kind, why, state, refs in raw["owned"]
                ]
            ),
            "queued": [
                [name, [self.of(arg) for arg in args], timed]
                for name, args, timed in raw["queued"]
            ],
            "keys_unique": len({call.request_key for call in calls}) == len(calls),
            "held_on_terminal": sorted(
                str(self.of(call))
                for call, _attempt, state in raw["slots"]
                if state == "reserved" and call in terminal
            ),
            "target_binding": binding,
        }


def _facts(
    caller: Caller, cut: str, after_cut: dict[str, Any], after_settle: dict[str, Any]
) -> dict[str, Any]:
    aliases = _Aliases(caller, [after_cut, after_settle])
    return {
        "caller": caller.name,
        "cut": cut,
        "bound": caller.target is not None,
        "retried": [aliases.of(call) for call in caller.retried],
        "passes": caller.passes,
        "performs": caller.performs,
        "skip": caller.skip,
        "after_cut": aliases.snapshot(after_cut),
        "after_settle": aliases.snapshot(after_settle),
    }


def _target(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    return next((call for call in snapshot["calls"] if call["id"] == "target"), None)


def _refs(aggregate: dict[str, Any]) -> list[object]:
    ref = aggregate["ref"]
    return [value for _outcome, value in ref] if isinstance(ref, list) else [ref]


def _new_intent(caller: str, cut: str) -> bool:
    """A guide click opens a new intent only past a refusal that sent nothing."""
    return caller == "guide" and "retry" not in cut


def _handoff(caller: str, cut: str) -> list[list[str]]:
    """The exact passes of a moved cell: first, any redelivery, then the settle."""
    refused = REFUSED[caller]
    new_intent = _new_intent(caller, cut)
    if cut == SKIP:
        first = SKIPPED[caller]
    elif caller == "sitfis" and "retry" in cut:
        first = "returned emitindo"  # `_run` ends there; the next advance projects it.
    else:
        first = refused
    passes = [["first", first]]
    if cut in {"moved-then-redelivery", SKIP}:
        # The skipped guide call is still today's open intent when redelivered.
        new = new_intent and cut != SKIP
        passes.append(["redeliver", "returned guia_obtida" if new else refused])
    if new_intent:
        obtained = passes[-1][1] == "returned guia_obtida"
        passes.append(
            ["settle", f"returned guia_{'existente' if obtained else 'obtida'}"]
        )
    else:
        passes.append(["settle", refused])
    return passes


def _moved(facts: dict[str, Any]) -> list[str]:  # noqa: C901, PLR0912 - one block per oracle
    caller, cut = facts["caller"], facts["cut"]
    after_cut, after_settle = facts["after_cut"], facts["after_settle"]
    first, last = _target(after_cut), _target(after_settle)
    if not facts["bound"] or first is None or last is None:
        return ["no target call was bound before the cut"]
    retry = "retry" in cut
    snapshots = (("after_cut", after_cut), ("after_settle", after_settle))
    problems = []
    # M1: the bound target is the recorded moved refusal with its frozen cause.
    want = [CallState.AUTHORITY, CallState.AUTHORITY, 1 if retry else 0]
    for name, snapshot in snapshots:
        row = _target(snapshot) or {}
        if [row.get("state"), row.get("code"), row.get("attempts")] != want:
            problems.append(f"M1 {name}: target {row}")
        if snapshot["target_binding"] is not True:
            problems.append(f"M1 {name}: the target row left its binding")
    observed = [row for row in after_cut["attempts"] if row[0] == "target"]
    if observed != ([UNBILLED_401] if retry else []):
        problems.append(f"M1: target observations {observed}")
    if retry and facts["retried"] != ["target"]:
        problems.append(f"M1: the retry claim was for {facts['retried']}")
    # M2: exactly its one unsent slot, released, and no budget moved.
    for name, snapshot in snapshots:
        slots = [row for row in snapshot["slots"] if row[0] == "target"]
        if slots != [["target", 1, "released"]]:
            problems.append(f"M2 {name}: target slots {slots}")
    alone = [call["id"] for call in after_cut["calls"]] == ["target"]
    if alone and after_cut["budget"] != [0, 0, 0]:
        problems.append(f"M2: budget moved {after_cut['budget']}")
    if first["settled"] not in {None, 0}:
        problems.append(f"M2: target settled {first['settled']}")
    # M3: every send is an attempt; nothing was sent unscripted.
    for name, snapshot in snapshots:
        attempts = sum(call["attempts"] for call in snapshot["calls"])
        if snapshot["sends"] != attempts or snapshot["unscripted"]:
            problems.append(
                f"M3 {name}: {snapshot['sends']} sends for {attempts} attempts, "
                f"{snapshot['unscripted']} unscripted"
            )
    # M4: the caller's exact handoff, observed at every layer it has.
    if facts["passes"] != _handoff(caller, cut):
        problems.append(f"M4: passes {facts['passes']}")
    if caller == "receipt":
        # The retry refusal ends inside perform, which returns the 401's result.
        expected = ["returned falhou"] if retry else []
        if facts["performs"] != expected:
            problems.append(f"M4: perform {facts['performs']}")
    if caller in {"page", "sitfis"} and _refs(after_settle["aggregate"]) != ["target"]:
        problems.append(f"M4: the settled record names {after_settle['aggregate']}")
    # M5: the target is never rewritten; a guide click past an unsent refusal
    # is a new intent on another call, every other delivery repeats the refusal.
    if first != last:
        problems.append(f"M5: target {first} became {last}")
    others = [call for call in after_settle["calls"] if call["id"] != "target"]
    if _new_intent(caller, cut):
        if [call["state"] for call in others] != [CallState.SUCCESS]:
            problems.append(f"M5: the new guide intent ended {others}")
    elif others:
        problems.append(f"M5: the redelivery made new calls {others}")
    if cut == SKIP and facts["skip"] != {
        "claim": "skipped",
        "errors": [],
        "unchanged": True,
        "released": True,
    }:
        problems.append(f"skip: {facts['skip']}")
    return problems


def _transient(facts: dict[str, Any]) -> list[str]:
    # T1: nothing recorded a refusal at claim; the next step is recorded only.
    recorded = [
        call
        for call in facts["after_cut"]["calls"]
        if call["state"] in {CallState.AUTHORITY, CallState.PAUSED}
    ]
    return [f"T1: claim recorded {recorded}"] if recorded else []


def _pinned(kind: str, facts: dict[str, Any]) -> list[str]:
    target = _target(facts["after_cut"])
    want = CallState.AUTHORITY if kind == PIN_AUTHORITY else CallState.PAUSED
    if target is None or target["state"] != want:
        return [f"pin: target {target}, expected {want}"]
    slots = [row for row in facts["after_cut"]["slots"] if row[0] == "target"]
    if kind == PIN_AUTHORITY and (
        not slots or {row[2] for row in slots} != {"released"}
    ):
        return [f"pin: target slots {slots}"]
    return []


def _judge(kind: str, facts: dict[str, Any]) -> None:
    """Judge one node's recorded facts by its class; pure, so the oracle is tested."""
    problems = []
    for name in ("after_cut", "after_settle"):
        snapshot = facts[name]
        if not snapshot["keys_unique"]:
            problems.append(f"{name}: a request key repeats")
        if snapshot["held_on_terminal"]:
            problems.append(f"{name}: reserved slot on {snapshot['held_on_terminal']}")
        if kind != RECORDED and snapshot["unscripted"]:
            problems.append(f"{name}: {snapshot['unscripted']} unscripted sends")
    if kind == MOVED:
        problems += _moved(facts)
    elif kind == TRANSIENT:
        problems += _transient(facts)
    elif kind in {PIN_AUTHORITY, PIN_PAUSED}:
        problems += _pinned(kind, facts)
    assert problems == [], f"{facts['caller']}[{facts['cut']}] {kind}: {problems}"


def _bind(caller: Caller, call_id: UUID) -> dict[str, object]:
    with tenant_context(caller.tenant_id):
        call = ProviderCall.objects.get(pk=call_id)
    return {
        "tenant": call.tenant_id,
        "client": call.client_id,
        "operation": call.operation,
        "request_key": call.request_key,
        "call": call.pk,
        "effect": call.business_effect_key,
    }


def _wrap(
    caller: Caller,
    target: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    before: Callable[[], None] | None = None,
    after: Callable[[object], None] | None = None,
) -> None:
    """Bind the first claimed call; cut around this module's first claim."""
    real = target.claim
    fired: list[bool] = []

    def claim_with_cut(tenant_id: UUID, call_id: UUID) -> object:
        if caller.target is None:
            caller.target = _bind(caller, call_id)
        elif target is not caller.module:
            caller.retried.append(call_id)
        if fired:
            return real(tenant_id, call_id)
        fired.append(True)
        if before is not None:
            before()
        ticket = real(tenant_id, call_id)
        if after is not None:
            after(ticket)
        return ticket

    monkeypatch.setattr(target, "claim", claim_with_cut)


def _move(caller: Caller) -> None:
    with tenant_context(caller.tenant_id):
        connection_id = ClientProviderLink.objects.get(pk=caller.link_id).connection_id
        ProviderConnection.objects.filter(pk=connection_id).update(
            version=F("version") + 1
        )


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


def _disable_link(caller: Caller) -> None:
    with tenant_context(caller.tenant_id):
        ClientProviderLink.objects.filter(pk=caller.link_id).update(status="revogada")


def _lose_termo(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("work", "parties"):
        module = import_module(f"apps.integrations.serpro.{name}")
        monkeypatch.setattr(module, "termo_in_force", lambda *_args: False)


def _demote(caller: Caller) -> None:
    Membership.objects.filter(user=caller.actor).update(
        role=TenantRole.STAFF_ACCOUNTANT
    )


def _target_rows(caller: Caller) -> tuple[object, ...]:
    assert caller.target is not None
    with tenant_context(caller.tenant_id):
        call = ProviderCall.objects.get(pk=cast("UUID", caller.target["call"]))
        slots = sorted(
            ProviderReservation.objects.filter(call=call).values_list(
                "attempt_number", "state"
            )
        )
    return (call.state, call.version, call.fence, call.lease_owner, slots)


class _Holder:
    """Another connection holds the target call row until it is released."""

    def __init__(self, caller: Caller) -> None:
        assert caller.target is not None
        self.errors: list[str] = []
        self.holding, self.done = threading.Event(), threading.Event()
        self.thread = threading.Thread(
            target=self._hold, args=(caller.tenant_id, caller.target["call"])
        )

    def _hold(self, tenant_id: UUID, call_id: UUID) -> None:
        try:
            with tenant_context(tenant_id), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '10s'")
                list(ProviderCall.objects.select_for_update().filter(pk=call_id))
                self.holding.set()
                if not self.done.wait(timeout=30):
                    self.errors.append("holder: never released")
        except Exception as error:  # noqa: BLE001 - every holder failure is recorded
            self.errors.append(f"holder: {type(error).__name__}: {error}")
        finally:
            connections.close_all()

    def start(self) -> None:
        self.thread.start()
        if not self.holding.wait(timeout=15):
            self.errors.append("holder: never held the call")

    def release(self) -> bool:
        self.done.set()
        self.thread.join(timeout=30)
        return not self.thread.is_alive()


def _skip(caller: Caller, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Move the version and hold the call through the first claim, which skips it.

    The first pass must return its exact outcome while the call is held, with no
    wait and no write; the guide's holder ends at the claim's return, because its
    settle locks the call. Returns the step that ends the hold after that pass.
    """
    held: dict[str, Any] = {}

    def release() -> None:
        holder: _Holder | None = held.pop("holder", None)
        if holder is not None:
            released = holder.release()
            # A claim that raised never reached `skipped`: that is recorded too.
            caller.skip = {
                **(caller.skip or {"claim": "never returned"}),
                "errors": holder.errors,
                "released": released,
            }

    def move_and_hold() -> None:
        _move(caller)
        holder = _Holder(caller)
        held["holder"] = holder
        holder.start()
        held["rows"] = _target_rows(caller)

    def skipped(ticket: object) -> None:
        caller.skip = {
            "claim": "skipped" if ticket is None else "claimed",
            "unchanged": _target_rows(caller) == held["rows"],
        }
        if caller.name == "guide":
            release()

    _wrap(caller, caller.module, monkeypatch, move_and_hold, skipped)
    return release


def _cut(  # noqa: C901 - one branch per cut
    caller: Caller, cut: str, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], None]:
    """Install the cut; return the step that ends it after the first pass."""
    protocol = import_module("apps.integrations.call_protocol")
    if "retry" in cut:
        caller.script_401()
    caller.script_ok()

    def move() -> None:
        _move(caller)

    def pause() -> None:
        caller.settings.SERPRO_PAUSED = True

    def both() -> None:
        move()
        pause()

    def die() -> None:
        raise SystemExit

    if cut == SKIP:
        return _skip(caller, monkeypatch)
    at_claim: dict[str, Callable[[], None]] = {
        "moved-before-claim": move,
        "moved-then-redelivery": move,
        "moved-paused-claim": both,
        "pause-claim": pause,
        "authority-revoked-at-claim": lambda: _revoke(caller),
        "link-disabled-at-claim": lambda: _disable_link(caller),
        "termo-lost-at-claim": lambda: _lose_termo(monkeypatch),
        "actor-lost-between-deliveries": die,  # The intent is persisted, unclaimed.
    }
    after_claim: dict[str, Callable[[], None]] = {
        "moved-after-claim": move,
        "moved-paused-dispatch": both,
        "pause-dispatch": pause,
        "lease-expiry": die,
    }
    at_retry = {"moved-before-retry-claim": move, "pause-retry-claim": pause}
    after_retry = {"moved-after-retry-claim": move, "pause-retry-dispatch": pause}
    base = cut.removeprefix("unpause-after-")
    later = after_claim.get(base)
    _wrap(
        caller,
        caller.module,
        monkeypatch,
        at_claim.get(base),
        None if later is None else lambda _ticket: later(),
    )
    if base in at_retry or base in after_retry:
        late = after_retry.get(base)
        _wrap(
            caller,
            protocol,
            monkeypatch,
            at_retry.get(base),
            None if late is None else lambda _ticket: late(),
        )
    if cut == "quota":
        with tenant_context(caller.tenant_id):
            ProviderBudget.objects.update(cap_microbrl=0)
    if cut == "actor-lost-before-call":
        _demote(caller)
    if cut in SCANNER_CUTS:
        scanning = import_module("apps.cycles.scanning")
        assert scanning.scan_required(caller.tenant_id)
        caller.settings.CLAMAV_ENABLED = False
    return lambda: None


def _drive(caller: Caller, cut: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Cut, run the first pass, snapshot; then settle unpaused and snapshot again."""
    ended = _cut(caller, cut, monkeypatch)
    if cut == SKIP:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
    try:
        caller.passes.append(["first", _outcome(caller.run)])
    finally:
        ended()
        if cut == SKIP:
            with connection.cursor() as cursor:
                cursor.execute("RESET lock_timeout")
    if cut.startswith("unpause-after-"):
        caller.settings.SERPRO_PAUSED = False
        caller.passes.append(["redeliver", _outcome(caller.redeliver)])
    if cut == "lease-expiry":
        with tenant_context(caller.tenant_id):
            ProviderCall.objects.filter(operation=caller.operation).update(
                lease_until=timezone.now() - timedelta(seconds=1)
            )
    if cut == "actor-lost-between-deliveries":
        _demote(caller)
    if cut in {"moved-then-redelivery", SKIP, "actor-lost-between-deliveries"}:
        caller.passes.append(["redeliver", _outcome(caller.redeliver)])
    after_cut = _snapshot(caller)
    caller.settings.SERPRO_PAUSED = False
    caller.passes.append(["settle", _outcome(caller.redeliver)])
    return _facts(caller, cut, after_cut, _snapshot(caller))


def _node(
    caller: Caller,
    cut: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    _observe(caller, monkeypatch)
    facts = _drive(caller, cut, monkeypatch)
    record_property("tuple", json.dumps(facts, sort_keys=True))
    _judge(CUTS[cut], facts)


def _page_caller(world: World, settings: SettingsWrapper) -> Caller:
    tasks = import_module("apps.integrations.serpro.tasks")
    tenant_id = world.termo.tenant.pk

    def latest() -> PaymentQuery | None:
        with tenant_context(tenant_id):
            return PaymentQuery.objects.order_by("-created_at").first()

    def redeliver() -> object:
        query = latest()
        if query is None:
            return None
        if query.settlement_owner is not None:
            lease_runs_out(world, query.pk)
        return tasks.run_payment_query_task(str(tenant_id), str(query.pk))

    def aggregate(baseline: dict[str, Any]) -> dict[str, object]:
        query = PaymentQuery.objects.order_by("-created_at").first()
        consulted = FederalConsultation.objects.exclude(
            pk__in=baseline["consultations"]
        ).order_by("consulted_at", "pk")
        return {
            "state": query.state if query else None,
            "code": query.failure_code if query else None,
            "ref": [[row.outcome, row.provider_ref] for row in consulted],
        }

    def named() -> dict[str, str]:
        query = latest()
        return {str(query.pk): "query"} if query else {}

    return Caller(
        name="page",
        module=payments,
        tenant_id=tenant_id,
        link_id=world.link.pk,
        operation="SP-PAG",
        settings=settings,
        actor=world.termo.actor,
        gateway=world.gateway,
        run=world.consult,
        redeliver=redeliver,
        sends=lambda: len(world.sent()),
        aggregate=aggregate,
        named=named,
        script_ok=lambda: world.script(page([item("DAS000000000045")])),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="pagtoweb",
    )


def _receipt_caller(
    world: World, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> Caller:
    world.script(page([item("DAS000000000045")]))
    world.consult()
    tenant_id = world.termo.tenant.pk
    with tenant_context(tenant_id):
        das = FederalPayment.objects.get()
    tasks = import_module("apps.integrations.serpro.tasks")
    args = (str(tenant_id), str(das.pk), str(world.termo.actor.pk))
    pdf = import_module("tests.integrations.test_pagtoweb").PDF
    issued = json.dumps({"pdf": __import__("base64").b64encode(pdf).decode()})

    def aggregate(_baseline: dict[str, Any]) -> dict[str, object]:
        call = ProviderCall.objects.filter(
            request_key=payments.receipt_request_key(das.pk)
        ).first()
        document = (
            Document.objects.filter(acquisition_receipt_ref=str(call.pk)).first()
            if call is not None
            else None
        )
        return {
            "state": call.state if call else None,
            "code": call.provider_code if call else None,
            "ref": document.acquisition_receipt_ref if document else None,
        }

    caller = Caller(
        name="receipt",
        module=payments,
        tenant_id=tenant_id,
        link_id=world.link.pk,
        operation="SP-COMP",
        settings=settings,
        actor=world.termo.actor,
        gateway=world.gateway,
        run=lambda: tasks.emit_payment_receipt_task(*args),
        redeliver=lambda: tasks.emit_payment_receipt_task(*args),
        sends=lambda: len(world.sent("/Emitir")),
        aggregate=aggregate,
        named=lambda: {str(das.pk): "payment"},
        script_ok=lambda: world.script(Reply(body=receipt_body(issued))),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="pagtoweb",
    )
    module = import_module(payments.__name__)
    real_perform = module.perform

    def observed(*perform_args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        # The receipt's own handler and perform's are observed apart, so one
        # cannot mask the other.
        try:
            result = real_perform(*perform_args, **kwargs)
        except BaseException as error:
            caller.performs.append(f"raised {type(error).__name__}: {error}")
            raise
        caller.performs.append(f"returned {result.state}")
        return result

    monkeypatch.setattr(module, "perform", observed)
    return caller


def _guide_caller(
    world: GuideWorld, settings: SettingsWrapper, peer: FakeSerpro
) -> Caller:
    pgmei = import_module("apps.integrations.serpro.pgmei")

    def click() -> object:
        return pgmei.obtain_das_guide(
            world.owner, world.tenant_id, world.cycle.pk, origin=peer.origin
        )

    def aggregate(_baseline: dict[str, Any]) -> dict[str, object]:
        for call in ProviderCall.objects.filter(operation="SP-DAS").order_by(
            "created_at"
        ):
            document = pgmei._guide_document(call)
            if document is not None:
                link = (
                    EvidenceLink.objects.filter(document=document)
                    .select_related("request")
                    .first()
                )
                return {
                    "state": link.request.state if link else None,
                    "code": None,
                    "ref": str(call.pk),
                }
        return {"state": None, "code": None, "ref": None}

    return Caller(
        name="guide",
        module=pgmei,
        tenant_id=world.tenant_id,
        link_id=world.link.pk,
        operation="SP-DAS",
        settings=settings,
        actor=world.owner,
        gateway=peer,
        run=click,
        redeliver=click,
        sends=lambda: len(peer.requests),
        aggregate=aggregate,
        named=lambda: {str(world.cycle.pk): "cycle"},
        script_ok=lambda: peer.replies.append(das_reply()),
        script_401=lambda: peer.replies.append(Reply(status=401)),
    )


def _sitfis_caller(
    world: Any,  # noqa: ANN401 - the SITFIS support world
    settings: SettingsWrapper,
    *,
    prepared: bool,
) -> Caller:
    mod = import_module("apps.integrations.serpro.sitfis")
    tasks = import_module("apps.integrations.serpro.sitfis_tasks")
    world.script(protocol_reply(30_000))
    report = world.advance(world.request())
    world.clock.advance(seconds=30)
    step = mod._decide(world.tenant_id, report.pk) if prepared else None
    assert step is not None or not prepared

    def run() -> object:
        mod._run(world.tenant_id, report.pk, step)
        return world.reload(report)

    def deliver() -> object:
        return tasks._advance(str(world.tenant_id), str(report.pk))

    def aggregate(_baseline: dict[str, Any]) -> dict[str, object]:
        current = world.reload(report)
        receipt = FederalConsultation.objects.get(pk=current.consultation_id)
        return {
            "state": current.state,
            "code": receipt.outcome,
            "ref": receipt.provider_ref or None,
        }

    emissions = {
        mod.emission_request_key(report.pk, attempt): attempt
        for attempt in range(1, mod.MAX_STEPS + 1)
    }
    return Caller(
        name="sitfis",
        module=mod,
        tenant_id=world.tenant_id,
        link_id=world.link.pk,
        operation=mod.EMISSION_OPERATION,
        settings=settings,
        actor=world.termo.actor,
        gateway=world.gateway,
        run=run if prepared else deliver,
        redeliver=deliver,
        sends=lambda: len(world.sent("/Emitir")),
        aggregate=aggregate,
        named=lambda: {
            str(report.pk): "report",
            str(report.consultation_id): "consultation",
            str(world.cycle.pk): "cycle",
        },
        # A non-zero wait: one advance never runs into an unscripted next send.
        script_ok=lambda: world.script(processing_202(4_000)),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="sitfis",
        ordinal=lambda call: (emissions.get(call.request_key, 0), str(call.pk)),
    )


@DB
@pytest.mark.parametrize("cut", [cut for cut in COMMON if cut != SKIP])
def test_page(
    world: World,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    cut: str,
) -> None:
    _node(_page_caller(world, settings), cut, monkeypatch, record_property)


@DB
@pytest.mark.parametrize("cut", [*COMMON, *ACTOR_CUTS])
def test_receipt(
    world: World,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    cut: str,
) -> None:
    caller = _receipt_caller(world, settings, monkeypatch)
    _node(caller, cut, monkeypatch, record_property)


@DB
@pytest.mark.parametrize(
    "cut", [cut for cut in COMMON if cut != "authority-revoked-at-claim"]
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
        _node(caller, cut, monkeypatch, record_property)


@DB
@pytest.mark.parametrize("cut", [*COMMON, *SCANNER_CUTS])
def test_sitfis(
    sitfis_world: Any,  # noqa: ANN401
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    cut: str,
) -> None:
    prepared = cut != "scanner-off-before-decision"
    caller = _sitfis_caller(sitfis_world, settings, prepared=prepared)
    _node(caller, cut, monkeypatch, record_property)


def _recorded_receipt_skip() -> dict[str, Any]:
    """The receipt's skip cell as the head run records it: the oracle's control."""
    target = {
        "id": "target",
        "state": CallState.AUTHORITY,
        "code": CallState.AUTHORITY,
        "attempts": 0,
        "billed": "desconhecido",
        "settled": None,
        "version": 2,
        "fence": 0,
        "owner": None,
        "lease": None,
        "started": None,
        "finalized": None,
        "retry_of": None,
    }
    snapshot = {
        "calls": [target],
        "attempts": [],
        "slots": [["target", 1, "released"]],
        "budget": [0, 0, 0],
        "sends": 0,
        "unscripted": 0,
        "aggregate": {
            "state": CallState.AUTHORITY,
            "code": CallState.AUTHORITY,
            "ref": None,
        },
        "owned": [],
        "queued": [],
        "keys_unique": True,
        "held_on_terminal": [],
        "target_binding": True,
    }
    return {
        "caller": "receipt",
        "cut": SKIP,
        "bound": True,
        "retried": [],
        "passes": [
            ["first", "returned reservada"],
            ["redeliver", "returned bloqueada_autoridade"],
            ["settle", "returned bloqueada_autoridade"],
        ],
        "performs": [],
        "skip": {"claim": "skipped", "errors": [], "unchanged": True, "released": True},
        "after_cut": deepcopy(snapshot),
        "after_settle": deepcopy(snapshot),
    }


def _lock_timeout_then_refused(facts: dict[str, Any]) -> None:
    facts["passes"][0] = [
        "first",
        "raised OperationalError: canceling statement due to lock timeout",
    ]
    facts["skip"]["claim"] = "never returned"


def _duplicate_slot(facts: dict[str, Any]) -> None:
    facts["after_cut"]["slots"].append(["target", 1, "released"])


def _terminal_rewrite(facts: dict[str, Any]) -> None:
    facts["after_settle"]["calls"][0]["version"] += 1


def _failed_redelivery(facts: dict[str, Any]) -> None:
    facts["passes"][-1] = ["settle", "raised RuntimeError: the worker was lost"]


def _authority_on_another_call(facts: dict[str, Any]) -> None:
    for name in ("after_cut", "after_settle"):
        calls = facts[name]["calls"]
        calls.append({**calls[0], "id": "successor"})
        calls[0]["state"] = CallState.CANCELLED


# The reviewer's counterfactuals (gate review :666) against a recorded cell.
COUNTERFACTUALS: Final[dict[str, Callable[[dict[str, Any]], None]]] = {
    "first-pass-lock-timeout": _lock_timeout_then_refused,
    "duplicate-slot": _duplicate_slot,
    "terminal-rewrite": _terminal_rewrite,
    "failed-final-redelivery": _failed_redelivery,
    "authority-on-another-call": _authority_on_another_call,
}


def test_the_oracle_accepts_the_recorded_skip_cell() -> None:
    _judge(MOVED, _recorded_receipt_skip())


@pytest.mark.parametrize("counterfactual", list(COUNTERFACTUALS))
def test_the_oracle_rejects(counterfactual: str) -> None:
    facts = _recorded_receipt_skip()
    COUNTERFACTUALS[counterfactual](facts)
    with pytest.raises(AssertionError):
        _judge(MOVED, facts)

"""AXRM-045 caller x cut matrix for the claim contract (re-scoped core-a-moved).

This part claims only what its edits change:

- MOVED, red on 1bd599f: a version move at the first or the retry claim records
  the target call, bound before the cut, as `bloqueada_autoridade` with the cause
  frozen at refusal, releases its unsent slot, sends nothing more, and the caller
  hands off exactly (oracles M1-M5 in `_moved`).
- TRANSIENT, red on 85ca1f5 (the 3440c9a tree) by design, because that tree
  recorded every claim refusal: a pause, an ordinary authority veto, a disabled
  link or a lapsed termo at claim, without a version move, leaves no call
  `bloqueada_autoridade` or `pausada` (oracle T1, judged at that claim's exit).
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
effect key)` when the caller first claims and records evidence at every boundary
that occurs: seeded; every claim's entry and exit, at both claim sites; every
public pass, the first one inside the holder's lifetime where a call is held; and
the final settle. Rows are compared raw and field by field within a run, and
across trees under first-appearance aliases with instants reduced to set or
unset. This proves state at those boundaries, not the absence of a
write-and-restore between them. A guide click is a new intent only past a
refusal that sent nothing (`pgmei.py:418-433`), so the guide's M5 is that a new
click leaves the target untouched.

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

import collections
import hashlib
import json
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID, uuid4

import pytest
from django.db import OperationalError, connection, connections, transaction
from django.db.models import F
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import EvidenceLink
from apps.cycles.models import Exception as CycleException
from apps.integrations import call_contracts
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
from apps.integrations.serpro.reauthentication import COMPLETE_401
from apps.integrations.serpro.work import CLIENT_ABSENT, TERMO_PREFIX
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
RECORDED_AT_CLAIM: Final = frozenset({CallState.AUTHORITY, CallState.PAUSED})
PAUSE_VETO: Final = f"raised AdmissionDeniedError: {CallState.PAUSED}"
AUTHORITY_VETO: Final = f"raised AdmissionDeniedError: {CallState.AUTHORITY}"
ANY_TERMO: Final = "any lapsed-termo block"
# The veto each transient cut must meet at its own claim: site, outcome, reason.
VETO: Final[dict[str, tuple[str, str, str]]] = {
    "pause-claim": ("claim", PAUSE_VETO, CallState.PAUSED),
    "pause-retry-claim": ("retry-claim", PAUSE_VETO, CallState.PAUSED),
    "moved-paused-claim": ("claim", PAUSE_VETO, CallState.PAUSED),
    "link-disabled-at-claim": ("claim", PAUSE_VETO, CallState.PAUSED),
    "authority-revoked-at-claim": ("claim", AUTHORITY_VETO, CLIENT_ABSENT),
    "termo-lost-at-claim": ("claim", AUTHORITY_VETO, ANY_TERMO),
}
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
# The definitive 401 a retry claim follows (`serpro/reauthentication.py:27-50`).
DEFINITIVE_401: Final = {
    "attempt_number": 1,
    "observation": "result",
    "outcome": CallState.FAILED,
    "http_status": 401,
    "provider_code": COMPLETE_401,
    "billed": "nao",
    "settled_microbrl": 0,
}
GUIDE_WORK: Final = "guia_das_nao_obtida"
REPORT_WORK: Final = "situacao_fiscal_nao_obtida"
FINAL_REPORT: Final = frozenset({"falha", "negado", "protocolo_expirado", "emitido"})

# Every concrete column of the three compared rows: nothing is left out by hand.
CALL_FIELDS: Final = tuple(f.attname for f in ProviderCall._meta.concrete_fields)
ATTEMPT_FIELDS: Final = tuple(
    f.attname for f in ProviderCallAttempt._meta.concrete_fields
)
SLOT_FIELDS: Final = tuple(f.attname for f in ProviderReservation._meta.concrete_fields)
BUDGET_FIELDS: Final = ("id", "tenant_id", "provider", "window_start")
MONEY: Final = ("reserved", "known_liability", "uncertain_exposure")
BUCKETS: Final = {"reserved": 0, "known": 1, "uncertain": 2}
OWNED: Final[dict[str, tuple[Any, tuple[str, ...]]]] = {
    "exception": (
        CycleException,
        (
            "id",
            "tenant_id",
            "client_id",
            "cycle_id",
            "subject_ref",
            "reason_code",
            "state",
            "owner_id",
            "review_on",
            "due_on",
            "version",
        ),
    ),
    "issue": (
        IntegrationIssue,
        (
            "id",
            "tenant_id",
            "client_id",
            "link_id",
            "service",
            "reason",
            "state",
            "owner_id",
            "review_on",
            "due_on",
            "evidence_refs",
            "version",
        ),
    ),
    "firm": (
        FirmIntegrationIssue,
        (
            "id",
            "tenant_id",
            "connection_id",
            "call_id",
            "reason",
            "state",
            "owner_id",
            "review_on",
            "evidence_refs",
            "version",
        ),
    ),
}
TOKEN_PATHS: Final = frozenset({"/authenticate", "/token"})
CONTROLS_PATH: Final = Path(__file__).with_name("call_admission_controls.json")
CONTROL_NODES: Final = (
    ("receipt", SKIP),
    ("receipt", "moved-before-retry-claim"),
    ("guide", "moved-then-redelivery"),
    ("page", "moved-before-claim"),
    ("receipt", "pause-retry-claim"),
)

Rows = dict[str, dict[str, Any]]
Changes = list[tuple[str, str, str]]


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
    aggregate: Callable[[Caller], dict[str, object]]
    named: Callable[[Caller], dict[str, str]]
    script_ok: Callable[[], None]
    script_401: Callable[[], None]
    authority_service: str = ""
    ordinal: Callable[[dict[str, Any]], tuple[object, ...]] = lambda row: (
        row["created_at"],
        str(row["id"]),
    )
    result_ids: Callable[[Caller, object], dict[str, object]] = lambda _c, _r: {}
    before_settle: Callable[[Caller], None] | None = None
    target: dict[str, object] | None = None
    pinned: dict[str, str] = field(default_factory=dict)
    decoys: set[str] = field(default_factory=set)
    claims: list[dict[str, Any]] = field(default_factory=list)
    passes: list[dict[str, Any]] = field(default_factory=list)
    stages: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    performs: list[str] = field(default_factory=list)
    queued: list[dict[str, Any]] = field(default_factory=list)
    issued: list[tuple[str, str, int]] = field(default_factory=list)
    unscripted: list[str] = field(default_factory=list)
    reserved: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    skip: dict[str, Any] | None = None
    seed: dict[str, Any] = field(default_factory=dict)
    pass_index: int = -1
    hooks: dict[
        str, tuple[Callable[[], None] | None, Callable[[object], None] | None]
    ] = field(default_factory=dict)
    fired: set[str] = field(default_factory=set)


# Pure helpers: each is exercised against counterfactuals below.


def _changed(before: Rows, after: Rows, prefix: str) -> Changes:
    """Every added, removed or differing field between two keyed row sets."""
    changes: Changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old is None or new is None:
            changes.append((prefix, key, "added" if old is None else "removed"))
            continue
        changes += [
            (prefix, key, name)
            for name in sorted(set(old) | set(new))
            if old.get(name) != new.get(name)
        ]
    return changes


def _target_changed(before: dict[str, Any], after: dict[str, Any]) -> Changes:
    return [
        change
        for part in ("calls", "attempts", "slots")
        for change in _changed(before[part], after[part], part)
    ]


def _attribute(
    issued: list[tuple[str, str, int]], tags: list[tuple[str, str | None]]
) -> dict[str, Any]:
    """Attribute each send the peer saw to the attempt that issued its tag."""
    owner: dict[str, tuple[str, int]] = {}
    conflicting = 0
    for tag, call, number in issued:
        if tag in owner and owner[tag] != (call, number):
            conflicting += 1
        owner[tag] = (call, number)
    attributed: dict[str, list[str]] = {}
    unattributed = 0
    for _path, sent in tags:
        if sent is not None and sent in owner:
            attributed.setdefault(owner[sent][0], []).append(sent)
        else:
            unattributed += 1
    seen = collections.Counter(tag for _path, tag in tags if tag is not None)
    duplicates = sum(count - 1 for count in seen.values()) + conflicting
    return {
        "attributed": attributed,
        "unattributed": unattributed,
        "duplicates": duplicates,
    }


def _ledger(slots: Rows) -> dict[str, dict[str, list[int]]]:
    """Each call's outstanding money per budget row, by bucket."""
    ledger: dict[str, dict[str, list[int]]] = {}
    for row in slots.values():
        if row["state"] in BUCKETS:
            entry = ledger.setdefault(str(row["budget_id"]), {}).setdefault(
                str(row["call_id"]), [0, 0, 0]
            )
            entry[BUCKETS[row["state"]]] += row["microbrl"]
    return ledger


def _queue_record(
    name: str,
    args: object,
    kwargs: object,
    options: dict[str, object],
    now: datetime,
) -> dict[str, Any]:
    """A queued continuation with its arguments, options and due time."""
    eta, countdown = options.get("eta"), options.get("countdown")
    due = round((eta - now).total_seconds()) if isinstance(eta, datetime) else countdown
    return {
        "task": name,
        "args": list(cast("tuple[object, ...]", args or ())),
        "kwargs": dict(cast("dict[str, object]", kwargs or {})),
        "options": {
            key: value
            for key, value in sorted(options.items())
            if key not in {"eta", "countdown"}
        },
        "due_in_s": due,
    }


def _recorder(sink: list[dict[str, Any]], name: str) -> Callable[..., None]:
    """Stand in for `apply_async`, recording each continuation in full."""

    def queue(args: object = (), kwargs: object = None, **options: object) -> None:
        sink.append(_queue_record(name, args, kwargs, options, timezone.now()))

    return queue


class _Aliases:
    """Name every within-run identity by its role or by its first appearance."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = dict(names)
        self.counts: dict[str, int] = {}

    def of(self, value: object, kind: str = "id") -> object:
        if value is None or value == "":
            return value
        raw = str(value)
        if raw not in self.names:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.names[raw] = f"{kind}#{self.counts[kind]}"
        return self.names[raw]

    def value(self, value: object) -> object:  # noqa: PLR0911 - one return per kind
        if isinstance(value, UUID):
            return self.of(value)
        if isinstance(value, (datetime, date)):
            return "set"
        if isinstance(value, str) and len(value) == 64 and _hexadecimal(value):
            return self.of(value, "hash")
        if isinstance(value, str) and len(value) == 36 and _uuid(value):
            return self.of(value)
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value

    def keyed(
        self, rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], str]
    ) -> Rows:
        return {key(row): cast("dict[str, Any]", self.value(row)) for row in rows}

    def changes(self, changes: Changes) -> list[str]:
        return [f"{part}.{self.of(key)}.{name}" for part, key, name in changes]


def _identity(value: object) -> bool:
    if isinstance(value, UUID):
        return True
    if isinstance(value, list):
        return any(_identity(item) for item in value)
    return isinstance(value, str) and len(value) == 36 and _uuid(value)


def _hexadecimal(value: str) -> bool:
    return all(char in "0123456789abcdef" for char in value)


def _uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


# Recording: a claim's entry and exit, every pass, and every snapshot.


def _rows(model: Any, fields: tuple[str, ...], **filters: object) -> Rows:  # noqa: ANN401
    return {
        str(row["id"]): row for row in model.objects.filter(**filters).values(*fields)
    }


def _budgets() -> Rows:
    return _rows(ProviderBudget, (*BUDGET_FIELDS, *MONEY))


def _target_raw(caller: Caller, call_id: object) -> dict[str, Any]:
    with tenant_context(caller.tenant_id):
        return {
            "calls": _rows(ProviderCall, CALL_FIELDS, pk=call_id),
            "attempts": _rows(ProviderCallAttempt, ATTEMPT_FIELDS, call_id=call_id),
            "slots": _rows(ProviderReservation, SLOT_FIELDS, call_id=call_id),
            "budgets": _budgets(),
            "aggregate": caller.aggregate(caller),
        }


def _tag(headers: tuple[tuple[str, str], ...]) -> str | None:
    value = next((v for k, v in headers if k.lower() == "x-request-tag"), None)
    return str(UUID(value)) if value else None


def _raw(caller: Caller) -> dict[str, Any]:
    """The whole compared world at one boundary, as raw rows."""
    with tenant_context(caller.tenant_id):
        calls = _rows(ProviderCall, CALL_FIELDS, operation=caller.operation)
        return {
            "calls": calls,
            "attempts": _rows(
                ProviderCallAttempt, ATTEMPT_FIELDS, call_id__in=list(calls)
            ),
            "slots": _rows(ProviderReservation, SLOT_FIELDS, call_id__in=list(calls)),
            "budgets": _budgets(),
            "owned": {kind: _rows(model, f) for kind, (model, f) in OWNED.items()},
            "aggregate": caller.aggregate(caller),
            "tags": [
                (request.path, _tag(request.headers))
                for request in caller.gateway.requests[caller.seed["requests"] :]
                if request.path not in TOKEN_PATHS
            ],
            "issued": list(caller.issued),
            "queued": deepcopy(caller.queued),
            "unscripted": len(caller.unscripted),
        }


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
    caller: Caller, module: ModuleType, site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record every claim at this site; cut around its first one, if hooked."""
    real = module.claim

    def claim_with_cut(tenant_id: UUID, call_id: UUID) -> object:
        if caller.target is None:
            caller.target = _bind(caller, call_id)
        before, after = caller.hooks.get(site, (None, None))
        cut = (before is not None or after is not None) and site not in caller.fired
        if cut:
            caller.fired.add(site)
            if before is not None:
                before()
        record: dict[str, Any] = {
            "site": site,
            "pass": caller.pass_index,
            "call": str(call_id),
            "cut": cut,
            "pre": _target_raw(caller, call_id),
        }
        caller.claims.append(record)
        try:
            ticket = real(tenant_id, call_id)
        except Exception as error:
            record.update(
                outcome=f"raised {type(error).__name__}: {error}",
                reason=getattr(error, "reason", ""),
                post=_target_raw(caller, call_id),
            )
            raise
        record.update(
            outcome="returned None" if ticket is None else "returned ticket",
            reason="",
            post=_target_raw(caller, call_id),
        )
        if cut and after is not None:
            after(ticket)
        return ticket

    monkeypatch.setattr(module, "claim", claim_with_cut)


def _describe(result: object) -> str:
    state = getattr(result, "state", None) or getattr(result, "code", None) or result
    return f"returned {state}"


def _pass(caller: Caller, kind: str, action: Callable[[], object]) -> None:
    """Run one public pass, then snapshot the world at its return."""
    caller.pass_index += 1
    with tenant_context(caller.tenant_id):
        before = {
            str(pk)
            for pk in ProviderCall.objects.filter(
                operation=caller.operation
            ).values_list("pk", flat=True)
        }
    caller.reserved.clear()
    caller.returned.clear()
    result: object = None
    try:
        result = action()
        outcome = _describe(result)
    except SystemExit:
        outcome = "died"
    except Exception as error:  # noqa: BLE001 - the outcome is what is recorded
        outcome = f"raised {type(error).__name__}: {error}"
    reserved = caller.reserved[-1] if caller.reserved else None
    target = str(caller.target["call"]) if caller.target is not None else None
    intent = (
        None
        if reserved is None
        else "target"
        if reserved == target
        else "new-click"
        if reserved not in before
        else "successor"
    )
    caller.passes.append(
        {
            "kind": kind,
            "outcome": outcome,
            "reserved": reserved,
            "intent": intent,
            "ids": caller.result_ids(caller, result),
        }
    )
    caller.stages.append((kind, _raw(caller)))


def _observe(caller: Caller, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record reservations, issued attempts, sends and continuations; seed."""
    gateway = caller.gateway
    real_reply = gateway.reply_for

    def counted(request: Any) -> Reply:  # noqa: ANN401 - the fake's own type
        if not gateway.replies:
            caller.unscripted.append(request.path)
        return real_reply(request)

    monkeypatch.setattr(gateway, "reply_for", counted)
    real_reserve = caller.module.reserve

    def reserved(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        row = real_reserve(*args, **kwargs)
        caller.reserved.append(str(row.pk))
        return row

    monkeypatch.setattr(caller.module, "reserve", reserved)
    protocol = import_module("apps.integrations.call_protocol")
    real_transmit = protocol.transmit

    def transmitted(dispatch: Any, adapter: Any) -> Any:  # noqa: ANN401
        receipt = dispatch.receipt
        caller.issued.append(
            (str(receipt.attempt_id), str(receipt.call_id), receipt.attempt_number)
        )
        return real_transmit(dispatch, adapter)

    monkeypatch.setattr(protocol, "transmit", transmitted)
    tasks = import_module("apps.integrations.serpro.tasks")
    sitfis_tasks = import_module("apps.integrations.serpro.sitfis_tasks")
    for task in (
        tasks.run_payment_query_task,
        tasks.emit_payment_receipt_task,
        sitfis_tasks.advance_situation_report_task,
    ):
        monkeypatch.setattr(
            task, "apply_async", _recorder(caller.queued, task.name.rsplit(".", 1)[-1])
        )
    _wrap(caller, caller.module, "claim", monkeypatch)
    _wrap(caller, protocol, "retry-claim", monkeypatch)
    caller.seed = {"requests": len(gateway.requests)}
    with tenant_context(caller.tenant_id):
        caller.seed["queries"] = {
            str(pk) for pk in PaymentQuery.objects.values_list("pk", flat=True)
        }
        caller.seed["consultations"] = {
            str(pk) for pk in FederalConsultation.objects.values_list("pk", flat=True)
        }
    caller.stages.append(("seeded", _raw(caller)))


# Cuts.


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

    def status(self) -> dict[str, object]:
        return {
            "ready": self.holding.is_set(),
            "alive": self.thread.is_alive(),
            "released": self.done.is_set(),
            "errors": list(self.errors),
        }

    def release(self) -> bool:
        self.done.set()
        self.thread.join(timeout=30)
        return not self.thread.is_alive()


def _probe(caller: Caller) -> str:
    """Whether another session still holds the target row, without waiting."""
    assert caller.target is not None
    try:
        with tenant_context(caller.tenant_id), transaction.atomic():
            list(
                ProviderCall.objects.select_for_update(nowait=True).filter(
                    pk=cast("UUID", caller.target["call"])
                )
            )
    except OperationalError:
        return "held"
    return "free"


def _held_changes(before: dict[str, Any], after: dict[str, Any]) -> Changes:
    changes = _target_changed(before, after)
    if before["aggregate"] != after["aggregate"]:
        changes.append(("aggregate", "-", "projection"))
    return changes


class _Held:
    """The held-call cut: the version moves and another runner holds the call."""

    def __init__(self, caller: Caller) -> None:
        self.caller = caller
        self.holder: _Holder | None = None
        self.in_cut: dict[str, Any] = {}

    def hold(self) -> None:
        _move(self.caller)
        self.holder = _Holder(self.caller)
        self.holder.start()
        assert self.caller.target is not None
        self.in_cut = _target_raw(self.caller, self.caller.target["call"])

    def check(self, through: str, first: str | None) -> None:
        """Record, while the call is still held, what the held pass left."""
        caller, holder = self.caller, self.holder
        assert caller.target is not None
        assert holder is not None
        claim = next(record for record in caller.claims if record["cut"])
        changes = _held_changes(self.in_cut, _target_raw(caller, caller.target["call"]))
        caller.skip = {
            "held_through": through,
            "claim": "skipped"
            if claim.get("outcome") == "returned None"
            else claim.get("outcome"),
            "first": first,
            "changed": changes,
            "probe": _probe(caller),
            "holder": holder.status(),
        }

    def release(self) -> None:
        if self.holder is not None and not self.holder.done.is_set():
            joined = self.holder.release()
            if self.caller.skip is not None:
                self.caller.skip["joined"] = joined


def _cut(caller: Caller, cut: str, monkeypatch: pytest.MonkeyPatch) -> _Held | None:  # noqa: C901 - one branch per cut
    """Install the cut at its claim site; return the held cut, if any."""
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
        held = _Held(caller)

        def at_return(_ticket: object) -> None:
            if caller.name == "guide":
                held.check("claim", None)
                held.release()

        caller.hooks["claim"] = (held.hold, at_return)
        return held
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
    if base in at_claim or base in after_claim:
        later = after_claim.get(base)
        caller.hooks["claim"] = (
            at_claim.get(base),
            None if later is None else lambda _ticket: later(),
        )
    if base in at_retry or base in after_retry:
        late = after_retry.get(base)
        caller.hooks["retry-claim"] = (
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
    return None


def _drive(caller: Caller, cut: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut, then run each pass with its own snapshot; environment steps between."""
    held = _cut(caller, cut, monkeypatch)
    if held is not None and caller.name != "guide":
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
        try:
            _pass(caller, "first", caller.run)
            held.check("first-pass", caller.passes[0]["outcome"])
        finally:
            held.release()
            with connection.cursor() as cursor:
                cursor.execute("RESET lock_timeout")
    else:
        _pass(caller, "first", caller.run)
    if cut.startswith("unpause-after-"):
        caller.settings.SERPRO_PAUSED = False
        _pass(caller, "repeat", caller.redeliver)
    if cut == "lease-expiry":
        with tenant_context(caller.tenant_id):
            ProviderCall.objects.filter(operation=caller.operation).update(
                lease_until=timezone.now() - timedelta(seconds=1)
            )
    if cut == "actor-lost-between-deliveries":
        _demote(caller)
    if cut in {"moved-then-redelivery", SKIP, "actor-lost-between-deliveries"}:
        _pass(caller, "repeat", caller.redeliver)
    caller.settings.SERPRO_PAUSED = False
    if caller.before_settle is not None:
        caller.before_settle(caller)
    _pass(caller, "settle", caller.redeliver)


# Facts: the recorded evidence, aliased for comparison across trees.


def _stage_target(raw: dict[str, Any], call: str) -> dict[str, Any]:
    return {
        "calls": {k: v for k, v in raw["calls"].items() if k == call},
        "attempts": {
            k: v for k, v in raw["attempts"].items() if str(v["call_id"]) == call
        },
        "slots": {k: v for k, v in raw["slots"].items() if str(v["call_id"]) == call},
    }


def _refusal(caller: Caller) -> int | None:
    target = str(caller.target["call"]) if caller.target is not None else None
    for index, record in enumerate(caller.claims):
        post = record.get("post", {}).get("calls", {}).get(record["call"], {})
        if (
            record["call"] == target
            and record.get("outcome", "").startswith("raised ")
            and record.get("outcome", "").endswith(f": {CallState.AUTHORITY}")
            and post.get("state") == CallState.AUTHORITY
        ):
            return index
    return None


class _Facts:
    """Normalize one node's raw records, in a fixed order, under aliases."""

    def __init__(self, caller: Caller) -> None:
        self.caller = caller
        names = {
            str(caller.tenant_id): "tenant",
            str(caller.link_id): "link",
            **caller.named(caller),
        }
        with tenant_context(caller.tenant_id):
            names[str(ClientProviderLink.objects.get(pk=caller.link_id).client_id)] = (
                "client"
            )
            budgets = ProviderBudget.objects.order_by("window_start", "pk")
            for n, pk in enumerate(budgets.values_list("pk", flat=True), 1):
                names[str(pk)] = f"budget#{n}"
        if caller.actor is not None:
            names[str(caller.actor.pk)] = "actor"
        target = str(caller.target["call"]) if caller.target is not None else None
        if target is not None:
            names[target] = "target"
        seen: list[str] = []
        for _kind, raw in caller.stages:
            for row in sorted(raw["calls"].values(), key=caller.ordinal):
                if str(row["id"]) not in seen:
                    seen.append(str(row["id"]))
        others = [key for key in seen if key != target]
        for n, key in enumerate(others, 1):
            stem = "call" if target is None else "successor"
            names[key] = stem if n == 1 and target is not None else f"{stem}#{n}"
        self.aliases = _Aliases(names)
        self.seed = caller.stages[0][1]["budgets"]

    def rows(self, raw: dict[str, Any]) -> dict[str, Any]:
        a, ordinal = self.aliases, self.caller.ordinal
        calls = sorted(raw["calls"].values(), key=ordinal)
        order = {str(row["id"]): n for n, row in enumerate(calls)}
        attempts = sorted(
            raw["attempts"].values(),
            key=lambda r: (
                order.get(str(r["call_id"]), 99),
                r["attempt_number"],
                r["observation"],
            ),
        )
        slots = sorted(
            raw["slots"].values(),
            key=lambda r: (order.get(str(r["call_id"]), 99), r["attempt_number"]),
        )
        return {
            "calls": {str(a.of(row["id"])): a.value(row) for row in calls},
            "attempts": a.keyed(
                attempts,
                lambda r: (
                    f"{a.of(r['call_id'])}/{r['attempt_number']}/{r['observation']}"
                ),
            ),
            "slots": a.keyed(
                slots, lambda r: f"{a.of(r['call_id'])}/{r['attempt_number']}"
            ),
            "budgets": {
                str(a.of(pk)): [row[m] - self.seed.get(pk, {}).get(m, 0) for m in MONEY]
                for pk, row in sorted(
                    raw["budgets"].items(), key=lambda i: (i[1]["window_start"], i[0])
                )
            },
        }

    def content(self, row: dict[str, Any]) -> str:
        """A row's order key without its own random id: known names, else '?'."""
        names = self.aliases.names
        return json.dumps(
            {
                key: names.get(str(value), "?") if _identity(value) else str(value)
                for key, value in row.items()
                if key != "id"
            },
            sort_keys=True,
        )

    def stage(self, raw: dict[str, Any]) -> dict[str, Any]:
        a = self.aliases
        rows = self.rows(raw)
        sends = _attribute(raw["issued"], raw["tags"])
        terminal = {
            key for key, row in rows["calls"].items() if row["state"] not in LIVE
        }
        owned = {}
        for kind, table in raw["owned"].items():
            for row in sorted(table.values(), key=self.content):
                owned[f"{kind}:{a.of(row['id'])}"] = a.value(row)
        return {
            **rows,
            "owned": owned,
            "aggregate": a.value(raw["aggregate"]),
            "sends": {
                "attributed": {
                    str(a.of(call)): a.value(tags)
                    for call, tags in sorted(
                        sends["attributed"].items(), key=lambda i: str(a.of(i[0]))
                    )
                },
                "unattributed": sends["unattributed"],
                "duplicates": sends["duplicates"],
                "unscripted": raw["unscripted"],
            },
            "queued": a.value(raw["queued"]),
            "keys_unique": len({row["request_key"] for row in rows["calls"].values()})
            == len(rows["calls"]),
            "held_on_terminal": sorted(
                key
                for key, row in rows["slots"].items()
                if row["state"] == "reserved" and str(row["call_id"]) in terminal
            ),
        }

    def facts(self, cut: str) -> dict[str, Any]:
        caller, a = self.caller, self.aliases
        stages = {
            f"{n}:{kind}": self.stage(raw)
            for n, (kind, raw) in enumerate(caller.stages)
        }
        claims = [
            {
                "site": record["site"],
                "pass": record["pass"],
                "call": a.of(record["call"]),
                "cut": record["cut"],
                "outcome": record.get("outcome", "never returned"),
                "reason": record.get("reason", ""),
                "changed": a.changes(_target_changed(record["pre"], record["post"]))
                if "post" in record
                else ["never returned"],
                "pre": self.rows(record["pre"]),
                "post": self.rows(record["post"]) if "post" in record else None,
            }
            for record in caller.claims
        ]
        refusal = _refusal(caller)
        since: dict[str, list[str]] = {}
        if refusal is not None:
            record = caller.claims[refusal]
            baseline = record["post"]
            for n, (kind, raw) in enumerate(caller.stages):
                if n - 1 >= record["pass"]:
                    since[f"{n}:{kind}"] = a.changes(
                        _target_changed(baseline, _stage_target(raw, record["call"]))
                    )
        skip = None
        if caller.skip is not None:
            skip = {
                **caller.skip,
                "changed": [
                    f"{part}.{a.of(key) if key != '-' else '-'}.{name}"
                    for part, key, name in caller.skip["changed"]
                ],
            }
        return {
            "caller": caller.name,
            "cut": cut,
            "kind": CUTS[cut],
            "bound": caller.target is not None,
            "claims": claims,
            "refusal": refusal,
            "since_refusal": since,
            "passes": [
                {
                    **record,
                    "reserved": a.of(record["reserved"]),
                    "ids": a.value(record["ids"]),
                }
                for record in caller.passes
            ],
            "performs": caller.performs,
            "stages": stages,
            "skip": skip,
        }


# The pure judge: what each class must show, read only from the facts.


def _stage_list(
    facts: dict[str, Any], from_pass: int
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, stage)
        for name, stage in facts["stages"].items()
        if int(name.split(":")[0]) - 1 >= from_pass
    ]


def _target_work(stage: dict[str, Any], subject: str) -> dict[str, Any]:
    return {
        key: row
        for key, row in stage["owned"].items()
        if row.get("subject_ref") == subject
        or subject in (row.get("evidence_refs") or [])
        or row.get("call_id") == subject
    }


def _successors(facts: dict[str, Any]) -> set[str]:
    return {p["reserved"] for p in facts["passes"] if p["intent"] == "new-click"}


def _handoff(caller: str, cut: str) -> list[list[object]]:
    """The exact passes of a moved cell: kind, outcome and intent."""
    refused = REFUSED[caller]
    no_send_guide = caller == "guide" and "retry" not in cut
    if cut == SKIP:
        first = SKIPPED[caller]
    elif caller == "sitfis" and "retry" in cut:
        first = "returned emitindo"  # `_run` ends there; the next advance projects it.
    else:
        first = refused
    # Page and SITFIS redeliveries of a finished record reserve nothing; a
    # receipt delivery and a guide click always reserve their intent.
    later = "target" if caller in {"receipt", "guide"} else None
    passes: list[list[object]] = [["first", first, "target"]]
    if cut == "moved-then-redelivery":
        passes.append(
            ["repeat", "returned guia_obtida", "new-click"]
            if no_send_guide
            else ["repeat", refused, later]
        )
    if cut == SKIP:
        # The skipped call is still open, so its redelivery claims and refuses it.
        passes.append(["repeat", refused, "target"])
    if no_send_guide:
        obtained = passes[-1][2] == "new-click"
        passes.append(
            ["settle", "returned guia_existente", "successor"]
            if obtained
            else ["settle", "returned guia_obtida", "new-click"]
        )
    else:
        passes.append(["settle", refused, later])
    return passes


def _m1(
    facts: dict[str, Any], snapshots: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    retry = "retry" in facts["cut"]
    problems = []
    for name, stage in snapshots:
        row = stage["calls"].get("target") or {}
        observed = [v for k, v in stage["attempts"].items() if k.startswith("target/")]
        if (row.get("state"), row.get("provider_code"), row.get("attempts")) != (
            CallState.AUTHORITY,
            CallState.AUTHORITY,
            1 if retry else 0,
        ):
            problems.append(f"M1: {name} target {row}")
        if retry:
            exact = len(observed) == 1 and all(
                observed[0].get(k) == v for k, v in DEFINITIVE_401.items()
            )
            if not exact or observed[0].get("attempt_id") != row.get("x_request_tag"):
                problems.append(f"M1: {name} 401 observations {observed}")
            if (row.get("http_status"), row.get("billed")) != (401, "nao"):
                problems.append(f"M1: {name} target http/billed")
        elif observed or (row.get("http_status"), row.get("billed")) != (
            None,
            "desconhecido",
        ):
            problems.append(f"M1: {name} unsent target observed {observed}")
    return problems


def _m2(  # noqa: C901 - one block per check
    facts: dict[str, Any],
    claim: dict[str, Any],
    later: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    retry = "retry" in facts["cut"]
    problems = []
    pre = list(claim["pre"]["slots"].values())
    post = list(claim["post"]["slots"].values())
    want_pre = "released" if retry else "reserved"
    if [(s["attempt_number"], s["state"]) for s in pre] != [(1, want_pre)]:
        problems.append(f"M2: slots at refusing claim entry {pre}")
    elif not retry and (
        pre[0]["microbrl"] <= 0
        or claim["pre"]["budgets"].get(pre[0]["budget_id"])
        != [pre[0]["microbrl"], 0, 0]
    ):
        problems.append(f"M2: claim entry budget {claim['pre']['budgets']}")
    if [(s["id"], s["attempt_number"], s["state"]) for s in post] != [
        (pre[0]["id"] if pre else None, 1, "released")
    ]:
        problems.append(f"M2: slots at refusal {post}")
    successors = _successors(facts) if facts["caller"] == "guide" else set()
    for name, stage in later:
        ledger = _ledger(stage["slots"])
        for budget, delta in stage["budgets"].items():
            entries = ledger.get(budget, {})
            total = [sum(v[i] for v in entries.values()) for i in range(3)]
            if delta != total:
                problems.append(f"M2: {name} {budget} moved {delta}, ledger {entries}")
            for call, money in entries.items():
                if call == "target":
                    if money != [0, 0, 0]:
                        problems.append(f"M2: {name} target holds {money}")
                    continue
                slots = [
                    s for k, s in stage["slots"].items() if k.startswith(f"{call}/")
                ]
                priced = (
                    call in successors
                    and len(slots) == 1
                    and slots[0]["state"] == "known"
                    and money == [0, slots[0]["microbrl"], 0]
                )
                if not priced and money != [0, 0, 0]:
                    problems.append(f"M2: {name} unexplained {call} {money}")
    return problems


def _m3(facts: dict[str, Any], later: list[tuple[str, dict[str, Any]]]) -> list[str]:
    retry = "retry" in facts["cut"]
    successors = _successors(facts) if facts["caller"] == "guide" else set()
    problems = []
    for name, stage in later:
        sends = stage["sends"]
        if sends["unattributed"] or sends["duplicates"] or sends["unscripted"]:
            problems.append(f"M3: {name} sends {sends}")
        tags = sends["attributed"].get("target", [])
        observed = [v for k, v in stage["attempts"].items() if k.startswith("target/")]
        want = [observed[0]["attempt_id"]] if retry and observed else []
        if tags != want:
            problems.append(f"M3: {name} target exchanges {tags}, expected {want}")
        for call, sent in sends["attributed"].items():
            row = stage["calls"].get(call) or {}
            if call != "target" and (
                call not in successors or len(sent) != row.get("attempts")
            ):
                problems.append(f"M3: {name} sends attributed to {call}: {sent}")
    return problems


def _m4(facts: dict[str, Any], later: list[tuple[str, dict[str, Any]]]) -> list[str]:  # noqa: C901, PLR0912 - one block per caller
    caller, cut = facts["caller"], facts["cut"]
    problems = []
    actual = [[p["kind"], p["outcome"], p["intent"]] for p in facts["passes"]]
    if actual != _handoff(caller, cut):
        problems.append(f"M4: passes {actual}")
    if any(stage["queued"] for stage in facts["stages"].values()):
        problems.append("M4: a continuation was queued")
    settle = facts["stages"][list(facts["stages"])[-1]]
    aggregate = settle["aggregate"]
    if caller == "receipt":
        if facts["performs"] != (["returned falhou"] if "retry" in cut else []):
            problems.append(f"M4: perform {facts['performs']}")
        if any(p["ids"].get("call") != "target" for p in facts["passes"]):
            problems.append("M4: a pass returned another stored call")
        for name, stage in later:
            got = stage["aggregate"]
            if (
                got.get("payment"),
                got.get("call"),
                got.get("document"),
                got.get("key_is_payment"),
            ) != (
                "payment",
                "target",
                None,
                True,
            ):
                problems.append(f"M4: {name} receipt record {got}")
    if caller == "page":
        consulted = aggregate.get("consultations")
        target = settle["calls"].get("target") or {}
        want = [
            {
                "client_id": "client",
                "kind": aggregate.get("kind"),
                "outcome": "falha",
                "provider_ref": "target",
                "scope_digest": target.get("request_digest"),
            }
        ]
        if (
            aggregate.get("query"),
            aggregate.get("state"),
            aggregate.get("queries"),
        ) != (
            "query",
            "falha",
            1,
        ) or aggregate.get("page_key_is_target") is not True:
            problems.append(f"M4: page record {aggregate}")
        if consulted != want:
            problems.append(f"M4: page consultation {consulted}")
    if caller == "sitfis" and (aggregate.get("state"), aggregate.get("ref")) != (
        "falha",
        "target",
    ):
        problems.append(f"M4: the settled report names {aggregate}")
    if caller == "guide":
        for record in facts["passes"]:
            if record["intent"] != "new-click":
                continue
            ids = record["ids"]
            if (
                ids.get("call") != record["reserved"]
                or ids.get("document_call") != record["reserved"]
                or ids.get("private") is not True
                or ids.get("document") is None
                or ids.get("link_document") != ids.get("document")
                or ids.get("link_cycle") != "cycle"
            ):
                problems.append(f"M4: the new guide {ids}")
    return problems


def _m5(facts: dict[str, Any], later: list[tuple[str, dict[str, Any]]]) -> list[str]:  # noqa: C901 - one block per caller
    caller = facts["caller"]
    problems = [
        f"M5: {name} rewrote {paths}"
        for name, paths in facts["since_refusal"].items()
        if paths
    ]
    if caller == "guide":
        first = _target_work(later[0][1], "target") if later else {}
        rows = list(first.values())
        if len(rows) != 1 or (rows[0]["reason_code"], rows[0]["state"]) != (
            GUIDE_WORK,
            "aberta",
        ):
            problems.append(f"M5: guide work {first}")
        for name, stage in later:
            if _target_work(stage, "target") != first:
                problems.append(f"M5: {name} guide work changed")
    elif caller == "sitfis":
        final = [
            (n, s) for n, s in later if s["aggregate"].get("state") in FINAL_REPORT
        ]
        first = _target_work(final[0][1], "report") if final else {}
        rows = list(first.values())
        if len(rows) != 1 or (rows[0]["reason_code"], rows[0]["state"]) != (
            REPORT_WORK,
            "aberta",
        ):
            problems.append(f"M5: report work {first}")
        for name, stage in final:
            if _target_work(stage, "report") != first:
                problems.append(f"M5: {name} report work changed")
    for name, stage in later:
        if caller != "guide" and _target_work(stage, "target"):
            problems.append(f"M5: {name} work bound to the target")
    return problems


def _moved(facts: dict[str, Any]) -> list[str]:
    cut = facts["cut"]
    refusal = facts["refusal"]
    if not facts["bound"] or refusal is None:
        return ["M1: no committed moved refusal of the bound target"]
    claim = facts["claims"][refusal]
    site = "retry-claim" if "retry" in cut else "claim"
    problems = []
    if (claim["site"], claim["call"], claim["pass"]) != (
        site,
        "target",
        1 if cut == SKIP else 0,
    ):
        problems.append(f"M1: refusal at {claim['site']} pass {claim['pass']}")
    later = _stage_list(facts, claim["pass"])
    problems += _m1(facts, [("refusal", claim["post"]), *later])
    if "retry" in cut:
        # At retry-claim entry the definitive 401 is already the one observation.
        entry = [
            v for k, v in claim["pre"]["attempts"].items() if k.startswith("target/")
        ]
        row = claim["pre"]["calls"].get("target") or {}
        exact = len(entry) == 1 and all(
            entry[0].get(k) == v for k, v in DEFINITIVE_401.items()
        )
        if not exact or entry[0].get("attempt_id") != row.get("x_request_tag"):
            problems.append(f"M1: retry entry observations {entry}")
    problems += _m2(facts, claim, later)
    problems += _m3(facts, later)
    problems += _m4(facts, later)
    problems += _m5(facts, later)
    if cut == SKIP:
        want: dict[str, object] = {
            "held_through": "claim" if facts["caller"] == "guide" else "first-pass",
            "claim": "skipped",
            "first": None if facts["caller"] == "guide" else SKIPPED[facts["caller"]],
            "changed": [],
            "probe": "held",
            "holder": {"ready": True, "alive": True, "released": False, "errors": []},
            "joined": True,
        }
        if facts["skip"] != want:
            problems.append(f"Mskip: {facts['skip']}")
    return problems


def _transient(facts: dict[str, Any]) -> list[str]:
    site, outcome, reason = VETO[facts["cut"].removeprefix("unpause-after-")]
    vetoes = [record for record in facts["claims"] if record["cut"]]
    if not facts["bound"] or len(vetoes) != 1:
        return [f"T1: the cut's veto was not observed once: {vetoes}"]
    veto = vetoes[0]
    reason_ok = (
        veto["reason"].startswith(TERMO_PREFIX)
        if reason == ANY_TERMO
        else veto["reason"] == reason
    )
    problems = []
    if (veto["site"], veto["call"], veto["outcome"]) != (
        site,
        "target",
        outcome,
    ) or not reason_ok:
        problems.append(
            f"T1: veto {veto['site']} {veto['call']} {veto['outcome']} {veto['reason']}"
        )
    if veto["changed"]:
        problems.append(f"T1: the vetoed claim changed {veto['changed']}")
    post = (veto["post"] or {}).get("calls", {}).get("target") or {}
    if post.get("state") in RECORDED_AT_CLAIM:
        problems.append(f"T1: the claim recorded {post.get('state')}")
    first = facts["stages"].get("1:first") or {"calls": {}}
    recorded = [
        k for k, row in first["calls"].items() if row["state"] in RECORDED_AT_CLAIM
    ]
    if recorded:
        problems.append(f"T1: first return holds recorded refusals {recorded}")
    return problems


def _pinned(kind: str, facts: dict[str, Any]) -> list[str]:
    first = facts["stages"].get("1:first") or {"calls": {}, "slots": {}}
    target = first["calls"].get("target")
    want = CallState.AUTHORITY if kind == PIN_AUTHORITY else CallState.PAUSED
    if target is None or target["state"] != want:
        return [f"pin: target {target}, expected {want}"]
    slots = [row for key, row in first["slots"].items() if key.startswith("target/")]
    if kind == PIN_AUTHORITY and (
        not slots or {row["state"] for row in slots} != {"released"}
    ):
        return [f"pin: target slots {slots}"]
    return []


def _judge(kind: str, facts: dict[str, Any]) -> None:
    """Judge one node's recorded facts by its class; pure, so the oracle is tested."""
    problems = []
    for name, stage in facts["stages"].items():
        if not stage["keys_unique"]:
            problems.append(f"H: {name} a request key repeats")
        if stage["held_on_terminal"]:
            problems.append(f"H: {name} reserved slot on {stage['held_on_terminal']}")
        if (
            kind in {TRANSIENT, PIN_AUTHORITY, PIN_PAUSED}
            and stage["sends"]["unscripted"]
        ):
            problems.append(
                f"H: {name} {stage['sends']['unscripted']} unscripted sends"
            )
    if kind == MOVED:
        problems += _moved(facts)
    elif kind == TRANSIENT:
        problems += _transient(facts)
    elif kind in {PIN_AUTHORITY, PIN_PAUSED}:
        problems += _pinned(kind, facts)
    tags = sorted({problem.split(":", 1)[0] for problem in problems})
    assert problems == [], (
        f"[{' '.join(tags)}] {facts['caller']}[{facts['cut']}] {kind}: {problems}"
    )


def _controls() -> dict[str, dict[str, Any]]:
    controls: dict[str, dict[str, Any]] = json.loads(CONTROLS_PATH.read_text())
    return controls


def _node(
    caller: Caller,
    cut: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> dict[str, Any]:
    _observe(caller, monkeypatch)
    _drive(caller, cut, monkeypatch)
    facts = _Facts(caller).facts(cut)
    record_property("tuple", json.dumps(facts, sort_keys=True, default=str))
    _judge(CUTS[cut], facts)
    # A control node's recorded facts are the oracle self-tests' control; on the
    # trees without the typed refusal its refusing claim differs by design.
    if (caller.name, cut) in CONTROL_NODES and hasattr(
        call_contracts, "VersionMovedError"
    ):
        assert (
            json.loads(json.dumps(facts, default=str))
            == _controls()[f"{caller.name}[{cut}]"]
        )
    return facts


# Callers.


def _page_caller(world: World, settings: SettingsWrapper) -> Caller:
    tasks = import_module("apps.integrations.serpro.tasks")
    tenant_id = world.termo.tenant.pk

    def redeliver() -> object:
        query_id = caller.pinned.get("query")
        if query_id is None:
            return None
        with tenant_context(tenant_id):
            query = PaymentQuery.objects.get(pk=query_id)
        if query.settlement_owner is not None:
            lease_runs_out(world, query.pk)
        return tasks.run_payment_query_task(str(tenant_id), str(query_id))

    def aggregate(caller: Caller) -> dict[str, object]:
        query_id = caller.pinned.get("query")
        query = PaymentQuery.objects.filter(pk=query_id).first() if query_id else None
        created = PaymentQuery.objects.exclude(
            pk__in=[*caller.seed.get("queries", ()), *caller.decoys]
        ).count()
        consulted = FederalConsultation.objects.exclude(
            pk__in=caller.seed.get("consultations", ())
        ).order_by("consulted_at", "pk")
        target = caller.target
        key_ok = (
            target["request_key"] == payments._page_key(query, 0)
            if query is not None and target is not None
            else None
        )
        return {
            "query": query_id,
            "state": query.state if query else None,
            "code": query.failure_code if query else None,
            "queries": created,
            "kind": consulted[0].kind if consulted else None,
            "consultations": list(
                consulted.values(
                    "client_id", "kind", "outcome", "provider_ref", "scope_digest"
                )
            ),
            "page_key_is_target": key_ok,
        }

    caller = Caller(
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
        aggregate=aggregate,
        named=lambda c: (
            {str(c.pinned["query"]): "query"} if "query" in c.pinned else {}
        ),
        script_ok=lambda: world.script(page([item("DAS000000000045")])),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="pagtoweb",
    )
    return caller


def _pin_query(caller: Caller, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capture the consultation's id when it is opened, before any runner dies."""
    real_open = payments.open_payment_query

    def opened(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        query = real_open(*args, **kwargs)
        caller.pinned.setdefault("query", str(query.pk))
        return query

    monkeypatch.setattr(payments, "open_payment_query", opened)


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
    key = payments.receipt_request_key(das.pk)

    def aggregate(caller: Caller) -> dict[str, object]:
        call = ProviderCall.objects.filter(request_key=key).first()
        document = (
            Document.objects.filter(acquisition_receipt_ref=str(call.pk)).first()
            if call is not None
            else None
        )
        target = caller.target
        return {
            "payment": das.pk,
            "call": call.pk if call else None,
            "state": call.state if call else None,
            "code": call.provider_code if call else None,
            "document": document.pk if document else None,
            "key_is_payment": target is not None and target["request_key"] == key,
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
        aggregate=aggregate,
        named=lambda _c: {str(das.pk): "payment"},
        script_ok=lambda: world.script(Reply(body=receipt_body(issued))),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="pagtoweb",
        result_ids=lambda c, _r: {"call": c.returned[-1] if c.returned else None},
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
    real_emit = tasks.emit_payment_receipt

    def emitted(*emit_args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        call = real_emit(*emit_args, **kwargs)
        caller.returned.append(str(call.pk) if call is not None else "none")
        return call

    monkeypatch.setattr(tasks, "emit_payment_receipt", emitted)
    return caller


def _guide_caller(
    world: GuideWorld, settings: SettingsWrapper, peer: FakeSerpro
) -> Caller:
    pgmei = import_module("apps.integrations.serpro.pgmei")

    def click() -> object:
        return pgmei.obtain_das_guide(
            world.owner, world.tenant_id, world.cycle.pk, origin=peer.origin
        )

    def aggregate(_caller: Caller) -> dict[str, object]:
        guides = []
        for call in ProviderCall.objects.filter(operation="SP-DAS").order_by(
            "created_at", "pk"
        ):
            document = pgmei._guide_document(call)
            link = (
                EvidenceLink.objects.filter(document=document)
                .select_related("request")
                .first()
                if document is not None
                else None
            )
            guides.append(
                {
                    "call": call.pk,
                    "document": document.pk if document else None,
                    "link": link.pk if link else None,
                    "cycle": link.request.cycle_id if link else None,
                }
            )
        return {"guides": guides}

    def result_ids(_caller: Caller, result: object) -> dict[str, object]:
        if not isinstance(result, pgmei.GuideOutcome):
            return {}
        with tenant_context(world.tenant_id):
            document = Document.objects.filter(pk=result.document_id).first()
            link = (
                EvidenceLink.objects.select_related("request")
                .filter(pk=result.link_id)
                .first()
            )
        return {
            "call": result.call_id,
            "document": result.document_id,
            "link": result.link_id,
            "document_call": document.acquisition_receipt_ref if document else None,
            "private": (not document.portal_visible) if document else None,
            "link_document": link.document_id if link else None,
            "link_cycle": link.request.cycle_id if link else None,
        }

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
        aggregate=aggregate,
        named=lambda _c: {str(world.cycle.pk): "cycle"},
        script_ok=lambda: peer.replies.append(das_reply()),
        script_401=lambda: peer.replies.append(Reply(status=401)),
        result_ids=result_ids,
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

    def aggregate(_caller: Caller) -> dict[str, object]:
        current = world.reload(report)
        receipt = FederalConsultation.objects.get(pk=current.consultation_id)
        return {
            "report": report.pk,
            "state": current.state,
            "outcome": receipt.outcome,
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
        aggregate=aggregate,
        named=lambda _c: {
            str(report.pk): "report",
            str(report.consultation_id): "consultation",
            str(world.cycle.pk): "cycle",
        },
        # A non-zero wait: one advance never runs into an unscripted next send.
        script_ok=lambda: world.script(processing_202(4_000)),
        script_401=lambda: world.script(Reply(status=401)),
        authority_service="sitfis",
        ordinal=lambda row: (emissions.get(row["request_key"], 0), str(row["id"])),
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
    caller = _page_caller(world, settings)
    _pin_query(caller, monkeypatch)
    _node(caller, cut, monkeypatch, record_property)


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


@DB
def test_the_page_record_is_the_query_it_opened(
    world: World,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    """Fixture negative: a newer, already final query never stands in for it."""

    def decoy(caller: Caller) -> None:
        with tenant_context(caller.tenant_id):
            query = PaymentQuery.objects.get(pk=caller.pinned["query"])
            query.pk = uuid4()
            query.request_key = hashlib.sha256(b"decoy").hexdigest()
            query.state, query.failure_code = "falha", "decoy"
            query.created_at = timezone.now() + timedelta(seconds=1)
            query.settlement_owner = query.settlement_until = None
            query.save(force_insert=True)
        caller.decoys.add(str(query.pk))

    caller = _page_caller(world, settings)
    caller.before_settle = decoy
    _pin_query(caller, monkeypatch)
    _observe(caller, monkeypatch)
    _drive(caller, "moved-before-claim", monkeypatch)
    facts = _Facts(caller).facts("moved-before-claim")
    record_property("tuple", json.dumps(facts, sort_keys=True, default=str))
    settle = facts["stages"][list(facts["stages"])[-1]]["aggregate"]
    assert (settle["query"], settle["code"], settle["queries"]) == (
        "query",
        facts["stages"]["1:first"]["aggregate"]["code"],
        1,
    )
    assert len(caller.decoys) == 1


# Oracle self-tests: the collector's helpers and the judge, each against the
# counterfactuals the reviews named.


def _row(fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: f"value-{n}" for n, name in enumerate(fields)}


@pytest.mark.parametrize(
    "fields",
    [CALL_FIELDS, ATTEMPT_FIELDS, SLOT_FIELDS],
    ids=["call", "attempt", "slot"],
)
def test_every_row_field_is_compared(fields: tuple[str, ...]) -> None:
    models = {
        CALL_FIELDS: ProviderCall,
        ATTEMPT_FIELDS: ProviderCallAttempt,
        SLOT_FIELDS: ProviderReservation,
    }
    assert set(fields) == {f.attname for f in models[fields]._meta.concrete_fields}
    base = _row(fields)
    for name in fields:
        changed = {**base, name: "other"}
        assert _changed({"k": base}, {"k": changed}, "rows") == [("rows", "k", name)]


def test_the_collector_sees_a_rewritten_status_epoch_and_update() -> None:
    before = {"t": {"http_status": 401, "execution_epoch": 1, "updated_at": 1}}
    after = {"t": {"http_status": 403, "execution_epoch": 2, "updated_at": 2}}
    assert _changed(before, after, "calls") == [
        ("calls", "t", "execution_epoch"),
        ("calls", "t", "http_status"),
        ("calls", "t", "updated_at"),
    ]
    assert _changed(before, {**before, "late": {"x": 1}}, "attempts") == [
        ("attempts", "late", "added")
    ]
    assert _changed(before, {}, "attempts") == [("attempts", "t", "removed")]


def test_the_collector_attributes_sends_by_tag() -> None:
    issued = [("tag-a", "target", 1), ("tag-b", "successor", 1)]
    assert _attribute(issued, [("/Emitir", "tag-b")])["attributed"] == {
        "successor": ["tag-b"]
    }
    assert _attribute(issued, [("/Emitir", "tag-z")])["unattributed"] == 1
    assert _attribute(issued, [("/Emitir", None)])["unattributed"] == 1
    assert (
        _attribute(issued, [("/Emitir", "tag-a"), ("/Emitir", "tag-a")])["duplicates"]
        == 1
    )


def test_the_collector_ledger_keeps_every_bucket() -> None:
    slots = {
        "a": {"budget_id": "b", "call_id": "c", "state": "reserved", "microbrl": 5},
        "k": {"budget_id": "b", "call_id": "c", "state": "known", "microbrl": 7},
        "r": {"budget_id": "b", "call_id": "d", "state": "released", "microbrl": 9},
    }
    assert _ledger(slots) == {"b": {"c": [5, 7, 0]}}


def test_the_recorder_keeps_kwargs_options_and_due_time() -> None:
    # The call shape of `sitfis_tasks.py:54-59`, positional and keyword.
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.get_current_timezone())
    later = now + timedelta(minutes=1)
    sink: list[dict[str, Any]] = []
    queue = _recorder(sink, "t")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(timezone, "now", lambda: now)
        queue(("a", "b"), {"failures": 1}, eta=later, ignore_result=True)
        queue(("a", "b"), kwargs={"failures": 1}, eta=later, ignore_result=True)
    positional, keyword = sink
    assert (
        positional
        == keyword
        == {
            "task": "t",
            "args": ["a", "b"],
            "kwargs": {"failures": 1},
            "options": {"ignore_result": True},
            "due_in_s": 60,
        }
    )
    assert _queue_record("t", ("a", "b"), None, {"eta": later}, now)["kwargs"] == {}
    assert (
        _queue_record("t", ("a", "b"), {"failures": 1}, {"eta": now}, now)["due_in_s"]
        == 0
    )


def _control(node: str) -> dict[str, Any]:
    return deepcopy(_controls()[node])


def _last(facts: dict[str, Any]) -> dict[str, Any]:
    stage: dict[str, Any] = facts["stages"][list(facts["stages"])[-1]]
    return stage


def _each_later(facts: dict[str, Any]) -> list[dict[str, Any]]:
    claim = facts["claims"][facts["refusal"]]
    return [stage for _n, stage in _stage_list(facts, claim["pass"])]


def _veto_missing(facts: dict[str, Any]) -> None:
    for record in facts["claims"]:
        record["cut"] = False


def _veto_wrong(facts: dict[str, Any]) -> None:
    next(r for r in facts["claims"] if r["cut"])["outcome"] = "returned ticket"


def _veto_recorded(facts: dict[str, Any]) -> None:
    veto = next(r for r in facts["claims"] if r["cut"])
    veto["post"]["calls"]["target"]["state"] = CallState.PAUSED


def _first_duplicate_rewrite(facts: dict[str, Any]) -> None:
    first = next(iter(facts["since_refusal"]))
    facts["since_refusal"][first] = ["calls.target.version"]


def _released_early(facts: dict[str, Any]) -> None:
    facts["skip"]["probe"] = "free"
    facts["skip"]["holder"]["released"] = True


def _changed_while_held(facts: dict[str, Any]) -> None:
    facts["skip"]["changed"] = ["calls.target.version"]


def _observation(facts: dict[str, Any], name: str, value: object) -> None:
    for stage in _each_later(facts):
        for key, row in stage["attempts"].items():
            if key.startswith("target/"):
                row[name] = value


def _late_observation(facts: dict[str, Any]) -> None:
    attempts = _last(facts)["attempts"]
    attempts["target/1/late"] = {**attempts["target/1/result"], "observation": "late"}


def _observation_missing(facts: dict[str, Any]) -> None:
    _last(facts)["attempts"].clear()


def _send_swap(facts: dict[str, Any]) -> None:
    for stage in facts["stages"].values():
        sends = stage["sends"]["attributed"]
        if "successor" in sends:
            sends["target"] = sends.pop("successor")


def _unattributed(facts: dict[str, Any]) -> None:
    _last(facts)["sends"]["unattributed"] = 1


def _budget_without_successor(facts: dict[str, Any]) -> None:
    budgets = _last(facts)["budgets"]
    budgets[next(iter(budgets))] = [0, 320_000, 0]


def _reserve_beside_successor(facts: dict[str, Any]) -> None:
    for stage in _each_later(facts):
        budget = next(iter(stage["budgets"]))
        stage["budgets"][budget][0] += 320_000


def _rewritten(name: str) -> Callable[[dict[str, Any]], None]:
    def mutate(facts: dict[str, Any]) -> None:
        facts["since_refusal"][list(facts["since_refusal"])[-1]] = [
            f"calls.target.{name}"
        ]

    return mutate


def _guide_work_removed(facts: dict[str, Any]) -> None:
    owned = _last(facts)["owned"]
    for key in [k for k, row in owned.items() if row.get("subject_ref") == "target"]:
        del owned[key]


def _wrong_link(facts: dict[str, Any]) -> None:
    next(p for p in facts["passes"] if p["intent"] == "new-click")["ids"][
        "link_document"
    ] = "id#999"


def _no_document(facts: dict[str, Any]) -> None:
    ids = next(p for p in facts["passes"] if p["intent"] == "new-click")["ids"]
    ids["document"] = ids["document_call"] = None


def _decoy_query(facts: dict[str, Any]) -> None:
    _last(facts)["aggregate"]["query"] = "id#777"


def _second_query(facts: dict[str, Any]) -> None:
    _last(facts)["aggregate"]["queries"] = 2


def _new_intent_as_repeat(facts: dict[str, Any]) -> None:
    next(p for p in facts["passes"] if p["intent"] == "new-click")["intent"] = "target"


def _queued(facts: dict[str, Any]) -> None:
    _last(facts)["queued"] = [
        {"task": "x", "args": [], "kwargs": {}, "options": {}, "due_in_s": 0}
    ]


RECEIPT_SKIP, RECEIPT_RETRY = f"receipt[{SKIP}]", "receipt[moved-before-retry-claim]"
GUIDE_SUCCESSOR, PAGE_MOVED = "guide[moved-then-redelivery]", "page[moved-before-claim]"
RECEIPT_T1 = "receipt[pause-retry-claim]"
COUNTERFACTUALS: Final[dict[str, tuple[str, Callable[[dict[str, Any]], None], str]]] = {
    "t1-veto-missing": (RECEIPT_T1, _veto_missing, "T1"),
    "t1-veto-wrong": (RECEIPT_T1, _veto_wrong, "T1"),
    "t1-recorded-then-overwritten": (RECEIPT_T1, _veto_recorded, "T1"),
    "first-duplicate-rewrite": (RECEIPT_RETRY, _first_duplicate_rewrite, "M5"),
    "holder-released-early": (RECEIPT_SKIP, _released_early, "Mskip"),
    "changed-while-held": (RECEIPT_SKIP, _changed_while_held, "Mskip"),
    "401-outcome-negada": (
        RECEIPT_RETRY,
        lambda f: _observation(f, "outcome", "negada"),
        "M1",
    ),
    "401-code-http": (
        RECEIPT_RETRY,
        lambda f: _observation(f, "provider_code", "serpro_http_401"),
        "M1",
    ),
    "late-observation": (RECEIPT_RETRY, _late_observation, "M1"),
    "observation-missing-at-settle": (RECEIPT_RETRY, _observation_missing, "M1"),
    "send-swap": (GUIDE_SUCCESSOR, _send_swap, "M3"),
    "unattributed-send": (RECEIPT_RETRY, _unattributed, "M3"),
    "budget-without-successor": (RECEIPT_RETRY, _budget_without_successor, "M2"),
    "reserve-beside-successor": (GUIDE_SUCCESSOR, _reserve_beside_successor, "M2"),
    "http-status-403": (RECEIPT_RETRY, _rewritten("http_status"), "M5"),
    "epoch-2": (RECEIPT_RETRY, _rewritten("execution_epoch"), "M5"),
    "updated-at-changed": (RECEIPT_RETRY, _rewritten("updated_at"), "M5"),
    "guide-work-removed": (GUIDE_SUCCESSOR, _guide_work_removed, "M5"),
    "wrong-guide-link": (GUIDE_SUCCESSOR, _wrong_link, "M4"),
    "successor-without-document": (GUIDE_SUCCESSOR, _no_document, "M4"),
    "decoy-page-query": (PAGE_MOVED, _decoy_query, "M4"),
    "second-page-query": (PAGE_MOVED, _second_query, "M4"),
    "new-intent-as-repeat": (GUIDE_SUCCESSOR, _new_intent_as_repeat, "M4"),
    "queued-continuation": (RECEIPT_RETRY, _queued, "M4"),
}


@pytest.mark.parametrize("node", [f"{c}[{cut}]" for c, cut in CONTROL_NODES])
def test_the_oracle_accepts_its_control(node: str) -> None:
    # The guide control has a priced successor, so a blanket zero balance fails.
    _judge(CUTS[node.split("[")[1].rstrip("]")], _control(node))


@pytest.mark.parametrize("counterfactual", list(COUNTERFACTUALS))
def test_the_oracle_rejects(counterfactual: str) -> None:
    node, mutate, tag = COUNTERFACTUALS[counterfactual]
    facts = _control(node)
    mutate(facts)
    with pytest.raises(AssertionError, match=rf"\[[^\]]*\b{tag}\b"):
        _judge(CUTS[node.split("[")[1].rstrip("]")], facts)

"""Compare the AXRM-045 claim matrix across 1bd599f (B), 85ca1f5 (R) and a head (H).

Usage:
    python -m tests.integrations.call_admission_compare B.xml R.xml H.xml OUT.json
    python -m tests.integrations.call_admission_compare --self-test

Each matrix node's recorded `tuple` is flattened to field paths (lists by
position). Between every pair of trees on which a node is green, the set of
differing `(path, old, new)` must equal exactly the rules this manifest names for
that node and pair: an extra delta, a missing one, a wrong value and a rule that
never applies all fail. The roster and the designed red partitions are checked
too: MOVED red only on B with M tags, TRANSIENT red only on R with the T1 tag,
everything else green everywhere. Exit 0 only when all of it holds.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FactsByNode = dict[str, dict[str, dict[str, Any] | None]]
TREES = ("B", "R", "H")
PAIRS = ("B-R", "B-H", "R-H")
MOVED = (
    "moved-before-claim",
    "moved-before-retry-claim",
    "moved-then-redelivery",
    "moved-claim-skips-held-call",
)
TRANSIENT = (
    "pause-claim",
    "pause-retry-claim",
    "unpause-after-pause-claim",
    "unpause-after-pause-retry-claim",
    "moved-paused-claim",
    "authority-revoked-at-claim",
    "link-disabled-at-claim",
    "termo-lost-at-claim",
)
PINS = ("moved-after-claim", "moved-after-retry-claim", "moved-paused-dispatch")
RECORDED = (
    "pause-dispatch",
    "pause-retry-dispatch",
    "unpause-after-pause-dispatch",
    "unpause-after-pause-retry-dispatch",
    "lease-expiry",
    "quota",
)
CALLERS = {
    "page": [*MOVED[:3], *TRANSIENT, *PINS, *RECORDED],
    "receipt": [
        *MOVED,
        *TRANSIENT,
        *PINS,
        *RECORDED,
        "actor-lost-before-call",
        "actor-lost-between-deliveries",
    ],
    "guide": [
        *MOVED,
        *(c for c in TRANSIENT if c != "authority-revoked-at-claim"),
        *PINS,
        *RECORDED,
    ],
    "sitfis": [
        *MOVED,
        *TRANSIENT,
        *PINS,
        *RECORDED,
        "scanner-off-after-prepare",
        "scanner-off-before-decision",
    ],
}
AUTHORITY = "bloqueada_autoridade"


@dataclass(frozen=True)
class Rule:
    """One reviewed, stage-specific difference between two trees."""

    family: str
    node: str
    pair: str
    path: str
    old: object
    new: object


def _dispatch() -> list[Rule]:
    # Receipt dispatch cuts, first public return only: B raises, R and H return.
    states = {
        "moved-after-claim": AUTHORITY,
        "moved-after-retry-claim": AUTHORITY,
        "moved-paused-dispatch": "pausada",
        "pause-dispatch": "pausada",
        "pause-retry-dispatch": "pausada",
        "unpause-after-pause-dispatch": "pausada",
        "unpause-after-pause-retry-dispatch": "pausada",
    }
    return [
        Rule(
            "DISPATCH",
            f"test_receipt[{cut}]",
            pair,
            "passes.0.outcome",
            f"raised AdmissionDeniedError: {state}",
            f"returned {state}",
        )
        for cut, state in states.items()
        for pair in ("B-R", "B-H")
    ]


def _typed() -> list[Rule]:
    # E1/E2: the moved refusal is a VersionMovedError at H, the same state.
    old = f"raised AdmissionDeniedError: {AUTHORITY}"
    new = f"raised VersionMovedError: {AUTHORITY}"
    rules = [
        Rule("E1E2", f"test_receipt[{cut}]", pair, "performs.0", old, new)
        for cut in ("moved-after-claim", "moved-after-retry-claim")
        for pair in ("B-H", "R-H")
    ]
    refusing_claim = {
        # MOVED, R against H: the refusing claim record of each node.
        **{
            f"test_{c}[moved-before-claim]": 0
            for c in ("page", "receipt", "guide", "sitfis")
        },
        **{
            f"test_{c}[moved-then-redelivery]": 0
            for c in ("page", "receipt", "guide", "sitfis")
        },
        **{
            f"test_{c}[moved-before-retry-claim]": 1
            for c in ("page", "receipt", "guide", "sitfis")
        },
        **{
            f"test_{c}[moved-claim-skips-held-call]": 1
            for c in ("receipt", "guide", "sitfis")
        },
    }
    rules += [
        Rule("E1E2", node, "R-H", f"claims.{index}.outcome", old, new)
        for node, index in refusing_claim.items()
    ]
    return rules


def _lease() -> list[Rule]:
    return [
        Rule(
            "LEASE",
            "test_receipt[lease-expiry]",
            pair,
            "passes.1.outcome",
            "returned reivindicada",
            "returned incerta",
        )
        for pair in ("B-R", "B-H")
    ]


RULES: list[Rule] = [*_dispatch(), *_typed(), *_lease()]


def flatten(value: object, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out if value else {prefix: {}}
    if isinstance(value, list):
        out = {}
        for index, item in enumerate(value):
            out.update(flatten(item, f"{prefix}.{index}"))
        return out if value else {prefix: []}
    return {prefix: value}


def load(path: str) -> dict[str, dict[str, Any]]:
    nodes = {}
    root = ET.parse(path).getroot()  # noqa: S314 - this run's own JUnit
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        status, message = "passed", ""
        for child in case:
            if child.tag in {"failure", "error", "skipped"}:
                status, message = child.tag, child.get("message") or ""
        props = {p.get("name"): p.get("value") for p in case.iter("property")}
        nodes[name] = {
            "status": status,
            "message": message,
            "facts": json.loads(props["tuple"] or "null") if "tuple" in props else None,
        }
    return nodes


def kind(node: str) -> str | None:
    match = re.fullmatch(r"test_(page|receipt|guide|sitfis)\[(.+)\]", node)
    if match is None:
        return None
    cut = match.group(2)
    return (
        "MOVED"
        if cut in MOVED
        else "TRANSIENT"
        if cut in TRANSIENT
        else "PIN"
        if cut in PINS
        else "RECORDED"
    )


def _tag_ok(kind: str | None, tag: str) -> bool:
    """MOVED red only by its M oracles; TRANSIENT red only by T1."""
    return tag.startswith("M") if kind == "MOVED" else tag == "T1"


def deltas(a: dict[str, Any], b: dict[str, Any]) -> set[tuple[str, str, str]]:
    fa, fb = flatten(a), flatten(b)
    missing = "<missing>"
    return {
        (path, json.dumps(fa.get(path, missing)), json.dumps(fb.get(path, missing)))
        for path in set(fa) | set(fb)
        if fa.get(path, missing) != fb.get(path, missing)
    }


def check_pairs(
    rules: list[Rule], facts: dict[str, dict[str, dict[str, Any] | None]]
) -> list[str]:
    """Every green pair of each node differs by exactly its rules; no rule is stale."""
    problems: list[str] = []
    used: set[Rule] = set()
    for node, by_tree in facts.items():
        for pair in PAIRS:
            left, right = pair.split("-")
            a, b = by_tree.get(left), by_tree.get(right)
            if a is None or b is None:
                continue
            expected = {r for r in rules if r.node == node and r.pair == pair}
            used |= expected
            want = {(r.path, json.dumps(r.old), json.dumps(r.new)) for r in expected}
            got = deltas(a, b)
            problems.extend(
                f"{node} {pair}: undeclared {d}" for d in sorted(got - want)
            )
            problems.extend(
                f"{node} {pair}: expected delta missing {d}" for d in sorted(want - got)
            )
    problems.extend(f"stale rule {rule}" for rule in rules if rule not in used)
    return problems


def compare(  # noqa: C901 - one block per checked property
    runs: dict[str, dict[str, dict[str, Any]]],
) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    names = [set(run) for run in runs.values()]
    if not all(n == names[0] for n in names):
        problems.append("the trees ran different node sets")
    roster = {f"test_{c}[{cut}]" for c, cuts in CALLERS.items() for cut in cuts}
    matrix = {n for n in names[0] if kind(n) is not None}
    if matrix != roster:
        problems.append(
            f"roster: missing {sorted(roster - matrix)}, "
            f"extra {sorted(matrix - roster)}"
        )
    if "test_page[moved-claim-skips-held-call]" in names[0]:
        problems.append("the page held-call carve-out is not kept")
    counts = {
        k: sum(kind(n) == k for n in matrix)
        for k in ("MOVED", "TRANSIENT", "PIN", "RECORDED")
    }
    if counts != {"MOVED": 15, "TRANSIENT": 31, "PIN": 12, "RECORDED": 28}:
        problems.append(f"partition counts {counts}")
    green: dict[str, dict[str, dict[str, Any] | None]] = {}
    for node in sorted(names[0]):
        k = kind(node)
        red_on = {"MOVED": {"B"}, "TRANSIENT": {"R"}}.get(k or "", set())
        green[node] = {}
        for tree in TREES:
            row = runs[tree][node]
            wanted = "failure" if tree in red_on else "passed"
            if row["status"] != wanted:
                problems.append(f"{node} on {tree}: {row['status']}, expected {wanted}")
            if row["status"] == "failure":
                match = re.search(r"\[([^\]]*)\]", row["message"])
                tags = set(match.group(1).split()) if match else set()
                if not tags or not all(_tag_ok(k, t) for t in tags):
                    problems.append(f"{node} on {tree}: red tags {sorted(tags)}")
            if k is not None and row["status"] == "passed":
                if row["facts"] is None:
                    problems.append(f"{node} on {tree}: no recorded tuple")
                green[node][tree] = row["facts"]
    problems += check_pairs(
        RULES, {n: f for n, f in green.items() if kind(n) is not None}
    )
    report = {
        "rules": [rule.__dict__ for rule in RULES],
        "counts": counts,
        "nodes": len(names[0]),
        "green_pairs": sum(
            1
            for by_tree in green.values()
            for pair in PAIRS
            if all(t in by_tree for t in pair.split("-"))
        ),
    }
    return report, problems


def _say(*parts: object) -> None:
    sys.stdout.write(" ".join(str(p) for p in parts) + "\n")


def self_test() -> int:
    """Five cases: an exact match passes; four broken manifests fail."""
    base: dict[str, Any] = {
        "passes": [{"outcome": "raised X"}],
        "stages": {"1:first": {"v": 1}},
    }
    new: dict[str, Any] = {
        "passes": [{"outcome": "returned X"}],
        "stages": {"1:first": {"v": 1}},
    }
    rule = Rule("T", "n", "B-H", "passes.0.outcome", "raised X", "returned X")
    facts: FactsByNode = {"n": {"B": base, "H": new}}
    undeclared: FactsByNode = {
        "n": {"B": base, "H": {**new, "stages": {"1:first": {"v": 2}}}}
    }
    cases = {
        "exact match": (check_pairs([rule], facts), False),
        "undeclared delta": (check_pairs([rule], undeclared), True),
        "wrong value": (
            check_pairs(
                [Rule("T", "n", "B-H", "passes.0.outcome", "raised X", "returned Y")],
                facts,
            ),
            True,
        ),
        "missing expected delta": (
            check_pairs([rule, Rule("T", "n", "B-H", "stages.1:first.v", 1, 2)], facts),
            True,
        ),
        "stale rule": (
            check_pairs([rule, Rule("T", "gone", "B-H", "p", 1, 2)], facts),
            True,
        ),
    }
    failed = 0
    for name, (problems, should_fail) in cases.items():
        ok = bool(problems) == should_fail
        failed += not ok
        verdict = "rejected" if problems else "accepted"
        _say("OK  " if ok else "FAIL", f"{name}: {verdict}", problems[:1])
    passed = len(cases) - failed
    _say(f"SELF-TEST {'passed' if not failed else 'failed'} ({passed}/{len(cases)})")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    b, r, h, out = argv
    report, problems = compare({"B": load(b), "R": load(r), "H": load(h)})
    report["problems"] = problems
    Path(out).write_text(json.dumps(report, indent=1, sort_keys=True, default=str))
    _say(json.dumps({k: report[k] for k in ("counts", "nodes", "green_pairs")}))
    _say(f"PROBLEMS {len(problems)}")
    for line in problems:
        _say("  ", line[:600])
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

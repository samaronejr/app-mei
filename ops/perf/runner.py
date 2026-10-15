"""Measure real HTTP reads inside an attested, constrained local Compose container."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import statistics
import sys
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Final, TypedDict

from django.db import connection

from ops.perf.configuration import (
    constrained_resources,
    production_memory_limits,
)
from ops.perf.results import budgets, capture_identity, percentile
from ops.perf.seats import read_targets, remove_sessions

MIN_REQUESTS: Final = 200
MAX_REQUESTS: Final = 400
SERVER_TARGET_MS: Final = 500
__all__ = ["percentile"]

if TYPE_CHECKING:
    from http.client import HTTPResponse

    from ops.perf.results import Verdict
    from ops.perf.seats import ReadTarget


class ReadSample(TypedDict):
    """A real HTTP response, including its actor and payload/query counts."""

    actor_index: int
    actor: str
    server_head: str
    server_ms: float
    gzip_html_bytes: int
    queries: int
    server_resources: dict[str, int]


class ReadMeasurement(TypedDict):
    """Every sample is retained so reported percentiles can be recomputed."""

    requests: int
    p50_ms: float
    p95_ms: float
    max_gzip_html_bytes: int
    server_ms: list[float]
    target_server_ms: int
    target_status: str
    server_resources: dict[str, int]
    host: str
    path: str
    actor_role: str
    scope: str
    samples: list[ReadSample]
    budgets: dict[str, Verdict]
    capture: dict[str, str | int]


def populated_body(target: ReadTarget, body: bytes) -> bool:
    """Reject a Hoje shell/error page rather than measure an empty success."""
    if target.witness not in body:
        return False
    if target.name == "hoje":
        total = re.search(rb'data-briefing-total="(\d+)"', body)
        return (
            total is not None
            and int(total[1]) > 0
            and b'id="briefing-error"' not in body
            and b'id="briefing-unavailable"' not in body
        )
    return True


def response_sample(
    response: HTTPResponse, body: bytes, actor_index: int, actor_name: str, head: str
) -> ReadSample:
    """Reject missing instrumentation instead of reporting zero or null."""
    timing = response.getheader("Server-Timing", "")
    queries = response.getheader("X-Perf-Queries")
    if not timing.startswith("app;dur=") or queries is None:
        message = "Missing server timing/query instrumentation."
        raise RuntimeError(message)
    server_head = response.getheader("X-Perf-Head", "")
    if server_head != head:
        message = "Server image RELEASE differs from root's capture head."
        raise RuntimeError(message)
    observed = json.loads(response.getheader("X-Perf-Resources", "{}"))
    resources = {
        key: int(observed[key]) for key in ("cpu_quota", "cpu_period", "memory_bytes")
    }
    return {
        "actor_index": actor_index,
        "actor": actor_name,
        "server_head": server_head,
        "server_ms": float(timing.removeprefix("app;dur=")),
        "gzip_html_bytes": len(gzip.compress(body, mtime=0)),
        "queries": int(queries),
        "server_resources": resources,
    }


def measure(
    target: ReadTarget,
    port: int,
    requests: int,
    evidence: Path,
    identity: dict[str, str | int],
) -> ReadMeasurement:
    """Drive the actual HTTP stack, reject redirects/errors, exclude one warmup."""
    samples: list[float] = []
    sizes: list[int] = []
    server_resources: dict[str, int] = {}
    raw: list[ReadSample] = []
    deadline = monotonic() + 600
    for index in range(requests + 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            message = f"{target.name}: exceeded the ten-minute per-read deadline."
            raise TimeoutError(message)
        with closing(
            HTTPConnection("127.0.0.1", port, timeout=min(30, remaining))
        ) as client:
            client.request(
                "GET",
                target.path,
                headers={
                    "Host": target.host,
                    "Cookie": target.cookies[index % len(target.cookies)],
                },
            )
            response = client.getresponse()
            body = response.read()
            if response.status != HTTPStatus.OK or not populated_body(target, body):
                message = f"{target.name}: status={response.status}, content rejected."
                raise RuntimeError(message)
            actor = index % len(target.cookies)
            sample = response_sample(
                response, body, actor, target.actor_name(actor), str(identity["head"])
            )
            server_resources = sample["server_resources"]
            if index:
                raw.append(sample)
                samples.append(sample["server_ms"])
                sizes.append(sample["gzip_html_bytes"])
                with evidence.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "read": target.name,
                                "actor_role": target.actor_role,
                                "scope": target.scope,
                                "capture": identity,
                                "sample": sample,
                            }
                        )
                        + "\n"
                    )
    p95 = percentile(samples, 0.95)
    verdicts = budgets(
        samples, [sample["queries"] for sample in raw], sizes, interactive=False
    )
    return {
        "requests": requests,
        "p50_ms": statistics.median(samples),
        "p95_ms": p95,
        "max_gzip_html_bytes": max(sizes),
        "server_ms": samples,
        "target_server_ms": SERVER_TARGET_MS,
        "target_status": (
            "within_target"
            if all(verdict["passed"] for verdict in verdicts.values())
            else "follow_up"
        ),
        "server_resources": server_resources,
        "host": target.host,
        "path": target.path,
        "actor_role": target.actor_role,
        "scope": target.scope,
        "samples": raw,
        "budgets": verdicts,
        "capture": identity,
    }


def main() -> None:
    """Refuse unsafe targets/resources before connecting and write measured results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--requests", type=int, default=MIN_REQUESTS)
    parser.add_argument(
        "--output", type=Path, default=Path(".evidence/AXR1-028-perf.json")
    )
    args = parser.parse_args()
    if not MIN_REQUESTS <= args.requests <= MAX_REQUESTS:
        parser.error("--requests must be between 200 and 400.")
    limits = production_memory_limits(Path("docker-compose.prod.yml"))
    resources = constrained_resources(Path("/sys/fs/cgroup"), limits["web"])
    identity = capture_identity(args.seed)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        role = cursor.fetchone()
    if role != ("app_runtime", False, False):
        message = f"Unsafe measurement database role: {role!r}."
        raise RuntimeError(message)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = args.output.with_suffix(".samples.jsonl")
    # Refuse to append a second invocation to an earlier run's samples.
    with evidence.open("x"):
        pass
    targets = read_targets(f"axorim-growth-{args.seed}")
    try:
        reads = {
            target.name: measure(target, args.port, args.requests, evidence, identity)
            for target in targets
        }
        if any(
            sample["server_resources"] != resources
            for result in reads.values()
            for sample in result["samples"]
        ):
            message = "Server resources differ from the attested Compose constraints."
            raise RuntimeError(message)
    finally:
        removed_sessions = remove_sessions(targets)
        connection.close()
    report = {
        "label": "constrained-local",
        "measured_at": datetime.now(UTC).isoformat(),
        "seed": args.seed,
        "fixture_clock": "2026-09-20T12:00:00Z",
        "growth_clients": 1_000,
        "resources": resources,
        "production_memory_limits_bytes": limits,
        "db_role": role,
        "server": {"workers": 2, "threads": 2, "worker_class": "gthread"},
        "method": (
            "Outer Django middleware start to non-streaming response end; network "
            "excluded; query-counter overhead included; one warmup/read; "
            "p50 median; p95 nearest rank; gzip offline (150 KB = 150,000 bytes). "
            "Separate local client/server containers; not a reference-host SLO."
        ),
        "workload": (
            "10 synthetic firm actors and 10 portal actors, round-robin; normal "
            "120/minute read policy and live authorization remain enabled. "
            "MFA sessions are prepared outside timing using the browser harness shape."
        ),
        "reads": reads,
        "interactive": {
            "status": "not_measured_by_http_driver",
            "command": "python -m tests.perf.measure_interactions",
        },
        "capture": identity,
        "raw_samples_path": str(evidence),
        "followups": [
            {"read": name, "budgets": result["budgets"], "requires_numbered_work": True}
            for name, result in reads.items()
            if result["target_status"] == "follow_up"
        ],
        "cleanup": {
            "sessions_removed": removed_sessions,
            "server": "Compose-owned; caller must run the documented down command",
        },
        "slo_claim": False,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    sys.stdout.write(
        json.dumps({name: result["p95_ms"] for name, result in reads.items()})
    )
    sys.stdout.write("\n")
    if report["followups"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

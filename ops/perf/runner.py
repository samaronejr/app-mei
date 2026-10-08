"""Measure real HTTP reads inside an attested, constrained local Compose container."""

import argparse
import gzip
import json
import math
import statistics
import sys
from contextlib import closing
from datetime import UTC, datetime
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from typing import Final, TypedDict

from django.db import connection

from ops.perf.configuration import (
    constrained_resources,
    production_memory_limits,
)
from ops.perf.seats import ReadTarget, read_targets, remove_sessions

MIN_REQUESTS: Final = 200
SERVER_TARGET_MS: Final = 500


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


def percentile(samples: list[float], fraction: float) -> float:
    """Use the nearest-rank definition, including the observed tail sample."""
    return sorted(samples)[math.ceil(len(samples) * fraction) - 1]


def measure(target: ReadTarget, port: int, requests: int) -> ReadMeasurement:
    """Drive the actual HTTP stack, reject redirects/errors, exclude one warmup."""
    samples: list[float] = []
    sizes: list[int] = []
    server_resources: dict[str, int] = {}
    for index in range(requests + 1):
        with closing(HTTPConnection("127.0.0.1", port, timeout=60)) as client:
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
            if response.status != HTTPStatus.OK or target.witness not in body:
                message = f"{target.name}: status={response.status}, content rejected."
                raise RuntimeError(message)
            timing = response.getheader("Server-Timing", "")
            if not timing.startswith("app;dur="):
                message = f"{target.name}: missing server timing middleware."
                raise RuntimeError(message)
            observed = json.loads(response.getheader("X-Perf-Resources", "{}"))
            server_resources = {
                key: int(observed[key])
                for key in ("cpu_quota", "cpu_period", "memory_bytes")
            }
            if index:
                samples.append(float(timing.removeprefix("app;dur=")))
                sizes.append(len(gzip.compress(body, mtime=0)))
    p95 = percentile(samples, 0.95)
    return {
        "requests": requests,
        "p50_ms": statistics.median(samples),
        "p95_ms": p95,
        "max_gzip_html_bytes": max(sizes),
        "server_ms": samples,
        "target_server_ms": SERVER_TARGET_MS,
        "target_status": "within_target" if p95 < SERVER_TARGET_MS else "follow_up",
        "server_resources": server_resources,
        "host": target.host,
        "path": target.path,
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
    if args.requests < MIN_REQUESTS:
        parser.error("--requests must be at least 200.")
    limits = production_memory_limits(Path("docker-compose.prod.yml"))
    resources = constrained_resources(Path("/sys/fs/cgroup"), limits["web"])

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
    targets = read_targets(f"axorim-growth-{args.seed}")
    try:
        reads = {
            target.name: measure(target, args.port, args.requests) for target in targets
        }
        if any(result["server_resources"] != resources for result in reads.values()):
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
            "excluded; one warmup/read; p50 median; p95 nearest rank; gzip offline. "
            "Separate local client/server containers; not a reference-host SLO."
        ),
        "workload": (
            "10 synthetic firm actors and 10 portal actors, round-robin; normal "
            "120/minute read policy and live authorization remain enabled. "
            "MFA sessions are prepared outside timing using the browser harness shape."
        ),
        "reads": reads,
        "interactive": {
            "status": "pending",
            "dependency": "T25 throttled click-to-swapped-content harness",
            "profile": {
                "downlink_mbps": 9,
                "rtt_ms": 170,
                "device": "mid-tier Android",
            },
            "p95_ms": None,
        },
        "followups": [
            {"read": name, "p95_ms": result["p95_ms"], "target_ms": SERVER_TARGET_MS}
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


if __name__ == "__main__":
    main()

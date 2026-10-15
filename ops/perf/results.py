"""Shared machine-readable target verdicts; never substitute a verdict for samples."""

import math
import os
import re
from pathlib import Path
from typing import Final, TypedDict

SERVER_MS: Final = 500
INTERACTIVE_MS: Final = 1_000
QUERIES: Final = 25
GZIP_BYTES: Final = 150_000


def percentile(samples: list[float], fraction: float) -> float:
    """Use the nearest-rank definition, including the observed tail sample."""
    return sorted(samples)[math.ceil(len(samples) * fraction) - 1]


class Verdict(TypedDict):
    """Retain actual values and strict/inclusive comparisons."""

    actual: float
    limit: int
    comparison: str
    passed: bool


def budgets(
    samples: list[float], queries: list[int], sizes: list[int], *, interactive: bool
) -> dict[str, Verdict]:
    """Apply S11.2 without hiding an observed overage."""
    limit = INTERACTIVE_MS if interactive else SERVER_MS
    p95 = percentile(samples, 0.95)
    return {
        "latency": {
            "actual": p95,
            "limit": limit,
            "comparison": "<",
            "passed": p95 < limit,
        },
        "queries": {
            "actual": max(queries),
            "limit": QUERIES,
            "comparison": "<=",
            "passed": max(queries) <= QUERIES,
        },
        "gzip_html_bytes": {
            "actual": max(sizes),
            "limit": GZIP_BYTES,
            "comparison": "<=",
            "passed": max(sizes) <= GZIP_BYTES,
        },
    }


def driver_cpu_max(cgroup: Path) -> str:
    """Record the driver's CPU limit, or why this host cannot attest one.

    A systemd user manager may delegate only some controllers (often memory and pids);
    without the cpu controller no user unit has `cpu.max`, so record the delegated
    controllers as the evidence instead of inventing a limit.
    """
    limit = cgroup / "cpu.max"
    if limit.exists():
        return limit.read_text().strip()
    controllers = cgroup.parent / "cgroup.subtree_control"
    delegated = controllers.read_text().strip() or "none"
    return f"unavailable: cpu controller not delegated (parent enables: {delegated})"


def capture_identity(seed: int) -> dict[str, str | int]:
    """Require the root-supplied exact head and record real driver cgroup limits."""
    head = os.environ["AXR1_PERF_HEAD"]
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        message = "AXR1_PERF_HEAD must be the full capture checkout SHA."
        raise ValueError(message)
    unified = next(
        line.removeprefix("0::")
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    cgroup = Path("/sys/fs/cgroup") / unified.lstrip("/")
    return {
        "head": head,
        "head_source": "root supplied; retain git status alongside the capture",
        "driver_cpu_max": driver_cpu_max(cgroup),
        "driver_memory_max": (cgroup / "memory.max").read_text().strip(),
        "driver_swap_max": (cgroup / "memory.swap.max").read_text().strip(),
        "db_memory_bytes": int(os.environ["AXR1_PERF_DB_MEMORY_BYTES"]),
        "redis_memory_bytes": int(os.environ["AXR1_PERF_REDIS_MEMORY_BYTES"]),
        "service_limit_source": "root docker inspect; retain inspection JSON",
        "seed": seed,
        "clients": 1_000,
        "cycles": 12_000,
        "business_clock": "2026-09-20T12:00:00Z",
        "clock_note": "seed data clock, not a frozen HTTP/browser wall clock",
    }

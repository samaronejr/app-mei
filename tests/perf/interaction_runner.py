"""Capture populated Hoje HTMX swaps on an existing constrained local server."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, TypedDict

from django.db import connection
from playwright.sync_api import Error, sync_playwright

from ops.perf.configuration import production_memory_limits
from ops.perf.results import budgets, capture_identity, percentile
from ops.perf.seats import read_targets, remove_sessions
from tests.browser.harness import (
    ANDROID_PROFILE,
    ANDROID_USER_AGENT,
    MOBILE,
    measure_briefing_swap,
    throttle_android,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Response

    from ops.perf.seats import ReadTarget


class InteractionSample(TypedDict):
    """Retain timing, actual response counts, actor and populated DOM witness."""

    actor_index: int
    actor: str
    server_head: str
    elapsed_ms: float
    server_ms: float
    queries: int
    gzip_html_bytes: int
    response_url: str
    populated_total: int
    server_resources: dict[str, int]


def mobile_context(
    browser: Browser, target: ReadTarget, actor: int, port: int
) -> BrowserContext:
    """Reuse T28's MFA sessions without persisting them into the result."""
    context = browser.new_context(
        viewport=MOBILE,
        user_agent=ANDROID_USER_AGENT,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2.625,
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        service_workers="block",
    )
    name, value = target.cookies[actor].split("=", 1)
    context.add_cookies(
        [{"name": name, "value": value, "url": f"http://{target.host}:{port}/"}]
    )
    return context


def response_counts(response: Response) -> tuple[float, int, int, dict[str, int]]:
    """Require real response instrumentation and the production server envelope."""
    headers = response.headers
    assert headers["x-perf-head"] == os.environ["AXR1_PERF_HEAD"]
    timing = headers["server-timing"]
    assert timing.startswith("app;dur="), timing
    observed = json.loads(headers["x-perf-resources"])
    resources = {
        key: int(observed[key]) for key in ("cpu_quota", "cpu_period", "memory_bytes")
    }
    limits = production_memory_limits(Path("docker-compose.prod.yml"))
    assert resources["cpu_quota"] == resources["cpu_period"]
    assert resources["memory_bytes"] == limits["web"]
    return (
        float(timing.removeprefix("app;dur=")),
        int(headers["x-perf-queries"]),
        len(gzip.compress(response.body(), mtime=0)),
        resources,
    )


def capture(
    browser: Browser, target: ReadTarget, args: argparse.Namespace
) -> list[InteractionSample]:
    """Exclude one warmup; rotate actors; checkpoint every measured swap."""
    raw: list[InteractionSample] = []
    deadline = monotonic() + 1_200
    identity = capture_identity(args.seed)
    for index in range(args.requests + 1):
        if monotonic() >= deadline:
            message = "Interactive capture exceeded its twenty-minute deadline."
            raise TimeoutError(message)
        actor = index % len(target.cookies)
        context = mobile_context(browser, target, actor, args.port)
        page = None
        try:
            page = context.new_page()
            page.set_default_timeout(15_000)
            throttle_android(page)
            response = page.goto(
                f"http://{target.host}:{args.port}/?escopo=team&periodo=2026-09",
                wait_until="load",
                timeout=30_000,
            )
            assert response is not None
            assert response.status == 200
            assert target.witness.decode() in page.locator("body").inner_text()
            page.locator("#briefing-filters").click()
            timing, swapped = measure_briefing_swap(
                page, "2026-08", absent_signal=args.absent_signal
            )
            server_ms, queries, size, resources = response_counts(swapped)
            sample: InteractionSample = {
                "actor_index": actor,
                "actor": target.actor_name(actor),
                "server_head": swapped.headers["x-perf-head"],
                **timing,
                "server_ms": server_ms,
                "queries": queries,
                "gzip_html_bytes": size,
                "server_resources": resources,
            }
            if index:
                raw.append(sample)
                with args.output.with_suffix(".samples.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "interaction": "hoje_period_filter",
                                "capture": identity,
                                "actor_role": target.actor_role,
                                "scope": target.scope,
                                "sample": sample,
                            }
                        )
                        + "\n"
                    )
            if index == args.requests:
                page.screenshot(
                    path=str(args.output.with_suffix(".png")),
                    full_page=True,
                    caret="initial",
                )
        except Error:
            if page is not None:
                page.screenshot(
                    path=str(args.output.with_suffix(".failure.png")),
                    full_page=True,
                    caret="initial",
                )
            raise
        finally:
            context.close()
    return raw


def main() -> None:
    """Write honest measured/control results and exit nonzero on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--absent-signal", action="store_true")
    args = parser.parse_args()
    if not 200 <= args.requests <= 400:
        parser.error("--requests must be between 200 and 400.")
    identity = capture_identity(args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.with_suffix(".samples.jsonl").open("x"):
        pass
    # Separate identities keep an immediately preceding HTTP capture's limiter
    # counters out of this workload, without disabling or clearing the policy.
    namespace = "perf-mobile-control" if args.absent_signal else "perf-mobile"
    targets = read_targets(f"axorim-growth-{args.seed}", actor_namespace=namespace)
    target = next(target for target in targets if target.name == "hoje")
    report: dict[str, object] = {
        "status": "incomplete",
        "capture": identity,
        "interaction": "hoje_period_filter",
        "actor_role": target.actor_role,
        "scope": target.scope,
        "host": target.host,
        "path": "/?escopo=team&periodo=2026-08",
        "profile": ANDROID_PROFILE,
        "measured_at": datetime.now(UTC).isoformat(),
        "method": "DOM click capture listener to exact populated htmx:afterSwap",
        "samples": [],
        "raw_samples_path": str(args.output.with_suffix(".samples.jsonl")),
        "control": args.absent_signal,
        "slo_claim": False,
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                raw = capture(browser, target, args)
                report["chromium_version"] = browser.version
            finally:
                browser.close()
        elapsed = [sample["elapsed_ms"] for sample in raw]
        verdicts = budgets(
            elapsed,
            [sample["queries"] for sample in raw],
            [sample["gzip_html_bytes"] for sample in raw],
            interactive=True,
        )
        report.update(
            status="measured",
            samples=raw,
            p50_ms=statistics.median(elapsed),
            p95_ms=percentile(elapsed, 0.95),
            budgets=verdicts,
            passed=all(verdict["passed"] for verdict in verdicts.values()),
            requires_numbered_work=not all(
                verdict["passed"] for verdict in verdicts.values()
            ),
        )
        if args.absent_signal:
            report.update(status="control_failed", passed=False)
    except (
        Error,
        AssertionError,
        TimeoutError,
        KeyError,
        ValueError,
        OSError,
    ) as error:
        report.update(
            status="failed",
            error=str(error),
            passed=False,
            requires_numbered_work=not args.absent_signal,
            control_observed=(
                args.absent_signal
                and "AXR1 missing exact populated HTMX swap" in str(error)
            ),
        )
    finally:
        report["sessions_removed"] = remove_sessions(targets)
        connection.close()
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    sys.stdout.write(
        json.dumps(
            {
                "output": str(args.output),
                "status": report["status"],
                "passed": report.get("passed", False),
            }
        )
        + "\n"
    )
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

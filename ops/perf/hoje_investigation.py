"""Separate ordinary request timing from intrusive complete-SQL diagnostics."""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from time import perf_counter_ns
from typing import TYPE_CHECKING, Final

import psycopg
from django.db import connection
from django.test import Client
from django.test.utils import (
    CaptureQueriesContext,
    setup_test_environment,
    teardown_test_environment,
)

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.cycles.models import ServiceCycle
from apps.tenants.models import Tenant
from ops.perf.configuration import constrained_resources, production_memory_limits
from ops.perf.hoje_sql import (
    SQLCapture,
    boundary_inputs,
    encode_parameter,
    explain_statement,
    slowest_selects,
)
from ops.perf.runner import MIN_REQUESTS, SERVER_TARGET_MS, percentile
from ops.perf.seats import read_targets, remove_cookie_sessions

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

    from ops.perf.seats import ReadTarget

QUERY_CAP: Final = 25


def _request(target: ReadTarget, index: int) -> _MonkeyPatchedWSGIResponse:
    client = Client(SERVER_NAME=target.host, raise_request_exception=False)
    name, value = target.cookies[index % len(target.cookies)].split("=", 1)
    client.cookies[name] = value
    return client.get(target.path)


def _validate(response: _MonkeyPatchedWSGIResponse, target: ReadTarget) -> None:
    if response.status_code != HTTPStatus.OK or target.witness not in response.content:
        message = f"Hoje rejected: status={response.status_code}, content witness."
        raise RuntimeError(message)


def _server_ms(response: _MonkeyPatchedWSGIResponse) -> float:
    timing = response.get("Server-Timing", "")
    if not timing.startswith("app;dur="):
        message = "Missing outer request timer."
        raise RuntimeError(message)
    return float(timing.removeprefix("app;dur="))


def _dataset(slug: str) -> dict[str, object]:
    tenant = Tenant.objects.get(slug=slug)
    with tenant_context(tenant.pk):
        clients = ClientCompany.objects.count()
        cycles = ServiceCycle.objects.count()
    if (clients, cycles) != (1_000, 12_000):
        message = f"Wrong populated growth seed: clients={clients}, cycles={cycles}."
        raise RuntimeError(message)
    return {
        "tenant_id": str(tenant.pk),
        "slug": slug,
        "clients": clients,
        "cycles": cycles,
        "fixture": "seed_axorim_demo --firms=growth --seed=42",
        "fixture_clock": "2026-09-20T12:00:00Z",
        "request_clock": "live; no application clock override",
        "seeded_by_this_command": False,
        "analyzed_by_this_command": False,
    }


def _timings(target: ReadTarget, requests: int) -> dict[str, object]:
    _validate(_request(target, 0), target)
    samples: list[float] = []
    sizes: list[int] = []
    for index in range(requests):
        response = _request(target, index)
        _validate(response, target)
        samples.append(_server_ms(response))
        sizes.append(len(gzip.compress(response.content, mtime=0)))
    p95 = percentile(samples, 0.95)
    return {
        "requests": requests,
        "server_ms": samples,
        "p50_ms": statistics.median(samples),
        "p95_ms": p95,
        "target_p95_ms": SERVER_TARGET_MS,
        "target_status": "within_target" if p95 < SERVER_TARGET_MS else "over_target",
        "max_gzip_html_bytes": max(sizes),
        "instrumented": False,
        "boundary": "ServerTimer: outer middleware to completed Django response",
        "transport": "in-process Django Client; not the T28 Gunicorn HTTP run",
    }


def _capture(target: ReadTarget) -> tuple[dict[str, object], SQLCapture]:
    setup_test_environment()
    try:
        with CaptureQueriesContext(connection) as queries:
            observer = SQLCapture(queries.initial_queries)
            with connection.execute_wrapper(observer):
                start = perf_counter_ns()
                response = _request(target, 0)
                request_ms = (perf_counter_ns() - start) / 1_000_000
    finally:
        teardown_test_environment()
    statements = [statement.evidence(queries) for statement in observer.statements]
    page = response.context.get("page") if response.context else None
    return (
        {
            "status_code": response.status_code,
            "content_witness": target.witness in response.content,
            "page_total": page.total if page is not None else None,
            "page_items": len(page.items) if page is not None else None,
            "failed_providers": (
                list(page.failed_providers) if page is not None else None
            ),
            "client_call_wall_ms": request_ms,
            "server_ms": _server_ms(response),
            "execute_wall_ms_sum": sum(
                statement.wall_ms for statement in observer.statements
            ),
            "instrumented": True,
            "query_count": len(queries),
            "query_cap": QUERY_CAP,
            "query_cap_status": (
                "within_cap" if len(queries) <= QUERY_CAP else "over_cap"
            ),
            "statements": statements,
            # Commit/rollback debug records may not pass through execute_wrapper.
            # Keep every record rather than silently dropping those wall times.
            "django_captured_queries": list(queries.captured_queries),
            "transaction_timing_note": (
                "Records without a matching capture_index use Django's rounded "
                "seconds in time; execute_wall_ms excludes catalog collection, "
                "result fetch/decoding, Python work and transaction completion."
            ),
        },
        observer,
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--requests", type=int, default=MIN_REQUESTS)
    parser.add_argument("--explain", type=int, default=3)
    args = parser.parse_args()
    if args.requests < MIN_REQUESTS or args.explain < 1:
        parser.error("--requests must be >=200 and --explain must be positive.")
    return args


def _write(output: Path, report: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=encode_parameter) + "\n")


def _plans(observer: SQLCapture, report: dict[str, object], output: Path) -> None:
    plans: list[dict[str, object]] = []
    report["plans"] = plans
    for statement in observer.statements:
        try:
            plans.append(explain_statement(statement))
        except psycopg.Error as error:
            plans.append(
                {
                    "capture_index": statement.log_index,
                    "status": "explain_failed",
                    "sqlstate": error.sqlstate,
                    "error": str(error),
                }
            )
        _write(output, report)


def main() -> None:
    """Write constrained-lane diagnostic evidence without causal claims."""
    args = _arguments()
    limits = production_memory_limits(Path("docker-compose.prod.yml"))
    resources = constrained_resources(Path("/sys/fs/cgroup"), limits["web"])
    inputs = boundary_inputs()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        role = cursor.fetchone()
    if role != ("app_runtime", False, False):
        message = f"Unsafe investigation role: {role!r}."
        raise RuntimeError(message)
    setup_start = perf_counter_ns()
    dataset = _dataset("axorim-growth-42")
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "incomplete",
        "head_supplied_by_operator": args.head,
        "label": "constrained-local-diagnostic",
        "measured_at": datetime.now(UTC).isoformat(),
        "resources": resources,
        "required_service_memory_bytes": limits,
        "dataset": dataset,
        "planner_inputs_before_run": inputs,
        "slo_claim": False,
        "causal_claim": False,
        "test_setup_in_request_timing": False,
    }
    issued: list[str] = []
    try:
        targets = read_targets("axorim-growth-42", issued)
        target = next(target for target in targets if target.name == "hoje")
        report["request"] = {"host": target.host, "path": target.path, "method": "GET"}
        report["actor_rotation"] = len(target.cookies)
        report["preparation_wall_ms"] = (perf_counter_ns() - setup_start) / 1_000_000
        report["ordinary_requests"] = _timings(target, args.requests)
        diagnostic, observer = _capture(target)
        report["diagnostic_request"] = diagnostic
        _write(args.output, report)
        observer.statements = slowest_selects(observer.statements, args.explain)
        _plans(observer, report, args.output)
        report["status"] = "captured_not_causally_verified"
    finally:
        report["sessions_removed"] = remove_cookie_sessions(issued)
        _write(args.output, report)
        connection.close()


if __name__ == "__main__":
    main()

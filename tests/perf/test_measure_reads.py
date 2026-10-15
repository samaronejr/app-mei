"""Fail-closed targets/resources and recomputable benchmark statistics."""

from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse

from ops.perf import timer
from ops.perf.configuration import (
    constrained_resources,
    production_memory_limits,
    require_local_targets,
)
from ops.perf.results import budgets, driver_cpu_max, percentile
from ops.perf.runner import populated_body
from ops.perf.seats import ReadTarget


@pytest.mark.parametrize(
    ("database", "redis", "production", "allowed"),
    [
        (
            "postgres://app_runtime@127.0.0.1/axr1_t28",
            "redis://localhost/0",
            "live",
            True,
        ),
        (
            "postgres://app_runtime@remote/axr1_t28",
            "redis://localhost/0",
            "live",
            False,
        ),
        ("postgres://app_runtime@localhost/live", "redis://localhost/0", "live", False),
        (
            "postgres://app_runtime@localhost/axr1_t28",
            "redis://remote/0",
            "live",
            False,
        ),
        (
            "postgres://app_test@localhost/axr1_t28",
            "redis://localhost/0",
            "live",
            False,
        ),
        (
            "postgres://app_runtime@localhost/axr1_t28",
            "redis://localhost/0",
            "axr1_t28",
            False,
        ),
    ],
)
def test_only_isolated_loopback_runtime_targets_are_admitted(
    database: str,
    redis: str,
    production: str,
    allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("REDIS_URL", redis)
    monkeypatch.setenv("POSTGRES_DB", production)
    if allowed:
        require_local_targets()
    else:
        with pytest.raises(ValueError, match="loopback"):
            require_local_targets()


@pytest.mark.parametrize(
    ("cpu", "memory", "allowed"),
    [
        ("100000 100000", "419430400", True),
        ("max 100000", "419430400", False),
        ("200000 100000", "419430400", False),
        ("100000 100000", "838860800", False),
    ],
)
def test_constrained_label_requires_actual_cgroup_limits(
    cpu: str, memory: str, allowed: bool, tmp_path: Path
) -> None:
    (tmp_path / "cpu.max").write_text(cpu)
    (tmp_path / "memory.max").write_text(memory)
    if allowed:
        observed = constrained_resources(tmp_path, 419_430_400)
        assert observed["cpu_quota"] == observed["cpu_period"]
        assert observed["memory_bytes"] == 419_430_400
    else:
        with pytest.raises(ValueError, match="exactly one CPU"):
            constrained_resources(tmp_path, 419_430_400)


def test_limits_follow_each_production_service_not_an_unrelated_limit(
    tmp_path: Path,
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  web:\n    mem_limit: 411m\n  worker:\n    mem_limit: 999m\n"
        "  db:\n    mem_limit: 322m\n  redis:\n    mem_limit: 97m\n"
    )
    assert production_memory_limits(compose) == {
        "web": 430_964_736,
        "db": 337_641_472,
        "redis": 101_711_872,
    }


def test_nearest_rank_p95_retains_the_observed_tail() -> None:
    assert percentile(list(reversed([float(n) for n in range(1, 201)])), 0.95) == 190


@pytest.mark.parametrize(
    ("elapsed", "interactive", "passed"),
    [
        (499.9, False, True),
        (500.0, False, False),
        (999.9, True, True),
        (1_000.0, True, False),
    ],
)
def test_capture_latency_uses_strict_budget(
    elapsed: float, interactive: bool, passed: bool
) -> None:
    """Given a boundary sample, reject equality rather than raising the ceiling."""
    raw = [elapsed] * 200
    verdicts = budgets(raw, [25] * 200, [150_000] * 200, interactive=interactive)
    assert verdicts["latency"]["passed"] is passed
    assert verdicts["queries"]["passed"]
    assert verdicts["gzip_html_bytes"]["passed"]
    assert raw == [elapsed] * 200


def test_capture_over_budget_control_keeps_raw_tail() -> None:
    """Given a payload/query outlier, reject it even below the latency p95."""
    samples = [100.0] * 199 + [2_000.0]
    verdicts = budgets(samples, [25] * 199 + [26], [150_001], interactive=True)
    assert verdicts["latency"]["passed"]
    assert not verdicts["queries"]["passed"]
    assert not verdicts["gzip_html_bytes"]["passed"]
    assert samples[-1] == 2_000.0


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        (b"Hoje", False),
        (b'Hoje data-briefing-total="0"', False),
        (b'Hoje data-briefing-total="2"', True),
        (b'Hoje data-briefing-total="2" id="briefing-unavailable"', False),
    ],
)
def test_http_capture_rejects_unpopulated_hoje(body: bytes, accepted: bool) -> None:
    target = ReadTarget("hoje", "synthetic.localhost", "/", ("unused",), b"Hoje")
    assert populated_body(target, body) is accepted


def test_timer_wraps_the_inner_response(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([10_000_000, 55_000_000])
    monkeypatch.setattr(timer, "perf_counter_ns", lambda: next(clock))
    response = timer.ServerTimer(lambda request: HttpResponse(b"complete"))(
        HttpRequest()
    )
    assert isinstance(response, HttpResponse)
    assert response.content == b"complete"
    assert response["Server-Timing"] == "app;dur=45.000000"


@pytest.mark.django_db
def test_timer_counts_real_inner_sql_executions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given an enabled query capture, count real SQL in the wrapped response."""
    monkeypatch.setattr(settings, "PERF_QUERY_CAPTURE", True, raising=False)
    monkeypatch.setattr(settings, "PERF_RESOURCE_ATTESTATION", False, raising=False)

    def inner(request: HttpRequest) -> HttpResponse:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return HttpResponse(b"complete")

    response = timer.ServerTimer(inner)(HttpRequest())
    assert response["X-Perf-Queries"] == "1"


def test_driver_cpu_max_records_why_a_limit_is_unavailable(tmp_path: Path) -> None:
    unit = tmp_path / "app.slice" / "capture.service"
    unit.mkdir(parents=True)
    (tmp_path / "app.slice" / "cgroup.subtree_control").write_text("memory pids\n")

    assert driver_cpu_max(unit) == (
        "unavailable: cpu controller not delegated (parent enables: memory pids)"
    )

    (unit / "cpu.max").write_text("100000 100000\n")
    assert driver_cpu_max(unit) == "100000 100000"

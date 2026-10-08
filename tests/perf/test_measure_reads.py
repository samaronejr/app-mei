"""Fail-closed targets/resources and recomputable benchmark statistics."""

from pathlib import Path

import pytest
from django.http import HttpRequest, HttpResponse

from ops.perf import timer
from ops.perf.configuration import (
    constrained_resources,
    production_memory_limits,
    require_local_targets,
)
from ops.perf.runner import percentile


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


def test_timer_wraps_the_inner_response(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([10_000_000, 55_000_000])
    monkeypatch.setattr(timer, "perf_counter_ns", lambda: next(clock))
    response = timer.ServerTimer(lambda request: HttpResponse(b"complete"))(
        HttpRequest()
    )
    assert isinstance(response, HttpResponse)
    assert response.content == b"complete"
    assert response["Server-Timing"] == "app;dur=45.000000"

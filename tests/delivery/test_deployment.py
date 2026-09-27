"""The real Compose interpolator carries kill switches to every process."""

import json
import os
import subprocess

import pytest

from tests.security.test_csrf_trusted_origins_contract import (
    PROD_COMPOSE,
    PROJECT_ROOT,
    THROWAWAY,
    _docker_compose_cli,
    _mandatory_variable_names,
)


@pytest.mark.parametrize(
    "overrides", [{}, {"DELIVERY_PAUSED": "False", "DELIVERY_DAILY_CAP": "7"}]
)
def test_production_processes_receive_delivery_controls(
    overrides: dict[str, str],
) -> None:
    environment = dict.fromkeys(_mandatory_variable_names(), THROWAWAY)
    environment.update(
        {"PATH": os.environ["PATH"], "CSRF_TRUSTED_ORIGINS": "", **overrides}
    )
    docker = _docker_compose_cli()
    assert docker is not None
    rendered = subprocess.run(  # noqa: S603 - fixed command with synthetic environment
        [
            docker,
            "compose",
            "--env-file",
            os.devnull,
            "--file",
            str(PROD_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    for name in ("web", "worker", "beat"):
        actual = services[name]["environment"]
        assert actual["DELIVERY_PAUSED"] == overrides.get("DELIVERY_PAUSED", "True")
        assert actual["DELIVERY_DAILY_CAP"] == overrides.get(
            "DELIVERY_DAILY_CAP", "100"
        )

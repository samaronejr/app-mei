"""An alive break and a harness failure must both fail the actual gate driver."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "tests/integrations/pgmei_gate_driver.sh"


@pytest.mark.parametrize(
    ("mode", "command_exit", "driver_exit", "failed_stages"),
    [
        ("mutations", 0, 1, 1),
        ("mutations", 1, 0, 0),
        ("mutations", 3, 1, 1),
        ("ordinary", 7, 1, 1),
    ],
    ids=["surviving-break", "killed-break", "broken-harness", "ordinary-failure"],
)
def test_driver_propagates_every_unexpected_stage_exit(
    tmp_path: Path,
    mode: str,
    command_exit: int,
    driver_exit: int,
    failed_stages: int,
) -> None:
    script = f"""
set -uo pipefail
source {shlex.quote(str(CORE))}
failed=0
mode={mode}
round=control
EV={shlex.quote(str(tmp_path))}
G=controlled_command
CI=()
A17_VARIANTS=capability
wait_window() {{ :; }}
identity() {{ :; }}
ports() {{ :; }}
controlled_command() {{ return {command_exit}; }}
if [ "$mode" = mutations ]; then
    run_mutations
else
    stage ordinary controlled_command
fi
echo "failed_stages=$failed"
exit $(( failed > 0 ? 1 : 0 ))
"""
    result = subprocess.run(
        ["/usr/bin/bash"],
        input=script,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == driver_exit, result.stdout + result.stderr
    assert f"failed_stages={failed_stages}" in result.stdout
    [receipt] = list(tmp_path.glob("*.exit"))
    assert receipt.read_text().strip() == str(command_exit)

"""Production compose must not deadlock on a fresh scheduler heartbeat."""

import re
from pathlib import Path

COMPOSE = (Path(__file__).resolve().parents[2] / "docker-compose.prod.yml").read_text()


def _service_block(name: str) -> str:
    match = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  \w|\Z)", COMPOSE)
    assert match, f"missing {name} service"
    return match.group(1)


def test_beat_starts_without_waiting_for_web_health() -> None:
    block = _service_block("beat")
    assert re.search(r"web:\s*\n\s+condition: service_started", block)


def test_web_healthcheck_allows_cold_start() -> None:
    block = _service_block("web")
    match = re.search(r"start_period:\s*(\d+)s", block)
    assert match
    assert int(match.group(1)) >= 400


def test_web_healthcheck_asserts_healthz_200() -> None:
    block = _service_block("web")
    assert re.search(r"urlopen\(.*?/healthz", block, re.DOTALL)
    assert re.search(r"status\s*==\s*200", block)


def test_caddy_starts_without_waiting_for_web_health() -> None:
    block = _service_block("caddy")
    assert re.search(r"web:\s*\n\s+condition: service_started", block)

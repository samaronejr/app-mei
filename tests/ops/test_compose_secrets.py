"""Inspect Compose's resolved service graph rather than trusting YAML formatting."""

import json
import subprocess
from pathlib import Path

import pytest

from apps.integrations.transport_types import JSONValue

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_SECRETS = {
    "serpro_contratante_cert",
    "serpro_contratante_key",
    "serpro_contratante_key_pass",
}


@pytest.fixture
def services() -> dict[str, JSONValue]:
    result = subprocess.run(  # noqa: S603 - fixed Docker config command, no lifecycle
        [
            "/usr/bin/docker",
            "compose",
            "-f",
            str(ROOT / "docker-compose.prod.yml"),
            "config",
            "--no-interpolate",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    configuration: JSONValue = json.loads(result.stdout)
    assert isinstance(configuration, dict)
    resolved = configuration["services"]
    assert isinstance(resolved, dict)
    return resolved


def test_contratante_pem_secrets_mount_only_inside_the_broker(
    services: dict[str, JSONValue],
) -> None:
    # Given Compose's fully resolved service and anchor definitions.
    assert "serpro-broker" in services
    # When each service's secret sources are inspected.
    for name, service in services.items():
        assert isinstance(service, dict)
        entries = service.get("secrets", [])
        assert isinstance(entries, list)
        sources: set[str] = set()
        targets: set[str] = set()
        for entry in entries:
            assert isinstance(entry, dict)
            source, target = entry["source"], entry["target"]
            assert isinstance(source, str)
            assert isinstance(target, str)
            sources.add(source)
            targets.add(target)
        # Then no inheritance or worker mount can widen certificate custody.
        if name == "serpro-broker":
            assert sources == PRIVATE_SECRETS
            assert targets == {
                "serpro_contratante_cert.pem",
                "serpro_contratante_key.pem",
                "serpro_contratante_key_pass",
            }
        else:
            assert not sources & PRIVATE_SECRETS
            assert not any(secret in json.dumps(service) for secret in PRIVATE_SECRETS)


def test_broker_has_no_public_port_and_has_a_bounded_readonly_service(
    services: dict[str, JSONValue],
) -> None:
    # Given the broker service that alone holds the key material.
    broker = services.get("serpro-broker")
    assert isinstance(broker, dict)
    # When its externally relevant limits and health contract are inspected.
    assert broker.get("ports", []) == []
    assert broker["mem_limit"] == "64m"
    assert broker["read_only"] is True
    environment = broker["environment"]
    assert isinstance(environment, dict)
    mode = environment["SERPRO_ENVIRONMENT"]
    assert isinstance(mode, str)
    assert mode in {
        "fake",
        "${SERPRO_ENVIRONMENT:-fake}",
    }
    health = broker["healthcheck"]
    assert isinstance(health, dict)
    assert "/healthz" in json.dumps(health["test"])
    # Then it is not reachable by a published host port or an unbounded service.


def test_only_web_and_worker_share_the_brokers_private_network(
    services: dict[str, JSONValue],
) -> None:
    # Given a dedicated internal endpoint, not a caddy-facing route.
    broker = services.get("serpro-broker")
    assert isinstance(broker, dict)
    networks = broker["networks"]
    assert isinstance(networks, dict)
    assert networks
    assert "default" not in networks
    # When peer membership is resolved by Compose.
    for name, service in services.items():
        assert isinstance(service, dict)
        peers = service.get("networks", {})
        assert isinstance(peers, dict)
        # Then only its two authenticated callers share its endpoint network.
        if set(networks) & set(peers):
            assert name in {"serpro-broker", "web", "worker"}

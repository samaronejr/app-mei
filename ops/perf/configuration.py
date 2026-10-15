"""Fail closed on remote databases, production names and unbounded resources."""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def require_local_targets() -> None:
    """Check URLs before Django opens a connection; only isolated lane DBs qualify."""
    database = urlsplit(os.environ["DATABASE_URL"])
    redis = urlsplit(os.environ["REDIS_URL"])
    production_name = os.environ.get("POSTGRES_DB") or "app_mei"
    name = database.path.removeprefix("/")
    if (
        database.hostname not in {"127.0.0.1", "localhost"}
        or redis.hostname not in {"127.0.0.1", "localhost"}
        or not name.startswith("axr1_")
        or name == production_name
        or database.username != "app_runtime"
    ):
        message = "Measurement requires a loopback axr1_ database as app_runtime."
        raise ValueError(message)


def production_memory_limits(compose: Path) -> dict[str, int]:
    """Read the three service limits from the committed production configuration."""
    text = compose.read_text()
    limits = {}
    for name in ("web", "db", "redis"):
        section = re.search(
            rf"^  {name}:\n(.*?)(?=^  \w|\Z)", text, re.MULTILINE | re.DOTALL
        )
        if section is None:
            message = f"Missing production service {name}."
            raise ValueError(message)
        match = re.search(r"^    mem_limit: (\d+)m$", section[1], re.MULTILINE)
        if match is None:
            message = f"Missing production memory limit for {name}."
            raise ValueError(message)
        limits[name] = int(match[1]) * 1_024 * 1_024
    return limits


def constrained_resources(root: Path, memory_bytes: int) -> dict[str, int]:
    """Attest Linux cgroup v2 limits; never label an unconstrained host constrained."""
    quota, period = (root / "cpu.max").read_text().split()
    memory = int((root / "memory.max").read_text().strip())
    if quota == "max" or int(quota) != int(period) or memory != memory_bytes:
        message = "Expected exactly one CPU and the production web memory limit."
        raise ValueError(message)
    return {"cpu_quota": int(quota), "cpu_period": int(period), "memory_bytes": memory}

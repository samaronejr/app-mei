"""Audit a production env file for leftover placeholders before a deploy.

Usage: check_env_prod.py <env-file> <example-file>

Exits 1 and lists offending key NAMES (never values) when any of these hold:

- a value is CHANGEME (case-insensitive);
- a non-empty value is byte-identical to the example's value for the same key,
  unless the key is in EXAMPLE_IDENTICAL_ALLOWLIST;
- a key present in the example is missing from the env file;
- SENTRY_DSN is non-empty and not URL-shaped (http|https scheme, a netloc
  containing '@', and a non-empty path).

Prints "OK <n> keys audited" and exits 0 otherwise. Stdlib only: this runs on
the deploy box under its system python3, not inside the project virtualenv.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

# Keys whose correct production value legitimately equals the example's: fixed
# settings, provider-neutral defaults, and tunables the example already pins.
# Everything else identical to the example is a placeholder that was never
# filled in.
EXAMPLE_IDENTICAL_ALLOWLIST: Final = frozenset(
    {
        "ACME_DNS_OPTION",
        "CONN_MAX_AGE",
        "DEFAULT_FROM_EMAIL",
        "DJANGO_SETTINGS_MODULE",
        "EMAIL_PORT",
        "EMAIL_TIMEOUT",
        "EMAIL_USE_SSL",
        "EMAIL_USE_TLS",
        "LOG_LEVEL",
        "OCI_S3_REGION",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "REDIS_URL",
        "SECURE_HSTS_SECONDS",
        "SENTRY_ENVIRONMENT",
        "SENTRY_TRACES_SAMPLE_RATE",
        "TLS_DIRECTIVE",
        "TRUSTED_PROXY_COUNT",
    }
)

KEY_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PLACEHOLDER: Final = "CHANGEME"


def _unquote(value: str) -> str:
    if len(value) > 1 and value.startswith(("'", '"')) and value.endswith(value[0]):
        return value[1:-1]
    return value


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines; later duplicates win, matching `source` semantics."""
    entries: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            continue
        entries[key] = _unquote(value.strip())
    return entries


def _is_url_shaped_dsn(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and "@" in parsed.netloc
        and bool(parsed.path.strip("/"))
    )


def audit(env: dict[str, str], example: dict[str, str]) -> list[str]:
    """Return one finding per violation, key names only, deterministically ordered."""
    findings: list[str] = []
    for key in sorted(env):
        value = env[key]
        if value.upper() == PLACEHOLDER:
            findings.append(f"{key}: placeholder value was never replaced")
            continue
        example_value = example.get(key)
        if (
            value
            and example_value is not None
            and value == example_value
            and key not in EXAMPLE_IDENTICAL_ALLOWLIST
        ):
            findings.append(f"{key}: value is identical to the example file")
    findings.extend(
        f"{key}: declared in the example but missing"
        for key in sorted(example)
        if key not in env
    )
    sentry_dsn = env.get("SENTRY_DSN", "")
    if (
        sentry_dsn
        and sentry_dsn.upper() != PLACEHOLDER
        and not _is_url_shaped_dsn(sentry_dsn)
    ):
        findings.append("SENTRY_DSN: non-empty but not URL-shaped")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Run the audit; return 0 on success, 1 on findings, 2 on unreadable input."""
    parser = argparse.ArgumentParser(
        description="Audit a production env file against its example for placeholders."
    )
    parser.add_argument(
        "env_file", type=Path, help="the deployed env file, e.g. .env.prod"
    )
    parser.add_argument("example_file", type=Path, help="the committed example file")
    args = parser.parse_args(argv)

    try:
        env_text = args.env_file.read_text(encoding="utf-8")
        example_text = args.example_file.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"check_env_prod: {exc}\n")
        return 2

    env = parse_env(env_text)
    example = parse_env(example_text)

    findings = audit(env, example)
    if findings:
        for finding in findings:
            sys.stderr.write(f"FAIL {finding}\n")
        return 1
    sys.stdout.write(f"OK {len(env)} keys audited\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

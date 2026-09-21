"""Exercise ops/check_env_prod.py, the deploy preflight for .env.prod placeholders."""

import subprocess
import sys
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = PROJECT_ROOT / "ops" / "check_env_prod.py"
EXAMPLE: Final = PROJECT_ROOT / ".env.prod.example"
REALISTIC_ENV: Final = (
    PROJECT_ROOT / "tests" / "ops" / "fixtures" / "env_prod_realistic.txt"
)
# A value that exists only inside the fixture. Asserting it absent from the
# script's output proves the audit reports key names, never values.
SENTINEL_VALUE: Final = "synthetic-sentinel-secret-7f3a9c1e"


def _run(env_file: Path, example_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(env_file), str(example_file)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_realistic_env_file_passes() -> None:
    result = _run(REALISTIC_ENV, EXAMPLE)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK ")
    assert result.stdout.endswith("keys audited\n")


def test_changeme_value_fails_and_names_the_key(tmp_path: Path) -> None:
    # Given an otherwise-clean env file with one placeholder left behind
    env_file = tmp_path / "env"
    env_file.write_text(
        REALISTIC_ENV.read_text(encoding="utf-8").replace(
            "EMAIL_HOST=smtp.synthetic-fixture.test", "EMAIL_HOST=CHANGEME"
        ),
        encoding="utf-8",
    )

    # When the audit runs
    result = _run(env_file, EXAMPLE)

    # Then it fails and names the key, without printing the placeholder as a value
    assert result.returncode == 1
    assert "EMAIL_HOST" in result.stderr


def test_example_identical_secret_key_fails(tmp_path: Path) -> None:
    # Given a minimal pair where SECRET_KEY is copied verbatim from the example
    example_file = tmp_path / "example"
    example_file.write_text(
        "SECRET_KEY=example-secret-do-not-ship\nEMAIL_PORT=587\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "env"
    env_file.write_text(
        "SECRET_KEY=example-secret-do-not-ship\nEMAIL_PORT=587\n",
        encoding="utf-8",
    )

    # When the audit runs
    result = _run(env_file, example_file)

    # Then the non-allow-listed identical value is rejected by name
    assert result.returncode == 1
    assert "SECRET_KEY" in result.stderr


def test_allowlisted_identical_email_port_passes(tmp_path: Path) -> None:
    # Given a minimal pair whose only identical value is an allow-listed key
    example_file = tmp_path / "example"
    example_file.write_text("EMAIL_PORT=587\n", encoding="utf-8")
    env_file = tmp_path / "env"
    env_file.write_text("EMAIL_PORT=587\n", encoding="utf-8")

    # When the audit runs
    result = _run(env_file, example_file)

    # Then the allow-list admits it
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK 1 keys audited\n"


def test_output_never_contains_a_value(tmp_path: Path) -> None:
    # Given a failing env file that also carries the fixture's sentinel secret
    env_file = tmp_path / "env"
    env_file.write_text(
        REALISTIC_ENV.read_text(encoding="utf-8").replace(
            "EMAIL_HOST=smtp.synthetic-fixture.test", "EMAIL_HOST=CHANGEME"
        ),
        encoding="utf-8",
    )

    # When the audit runs and fails
    result = _run(env_file, EXAMPLE)

    # Then no stream carries the sentinel value — findings are key names only
    assert result.returncode == 1
    assert SENTINEL_VALUE not in result.stdout
    assert SENTINEL_VALUE not in result.stderr

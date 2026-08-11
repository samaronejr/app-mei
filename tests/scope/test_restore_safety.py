"""Reject restore runbook commands that would leave database objects misowned."""

import re
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PG_RESTORE: Final = re.compile(r"(?<![A-Za-z0-9_-])pg_restore\b")
CREATEDB: Final = re.compile(r"(?<![A-Za-z0-9_-])createdb\b")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    logical: list[tuple[int, str]] = []
    parts: list[str] = []
    first_number = 1
    for number, physical_line in enumerate(text.splitlines(), 1):
        if not parts:
            first_number = number
        line = physical_line.rstrip()
        continued = line.endswith("\\")
        parts.append((line[:-1] if continued else line).strip())
        if not continued:
            logical.append((first_number, " ".join(parts)))
            parts = []
    if parts:
        logical.append((first_number, " ".join(parts)))
    return logical


def restore_safety_findings(text: str) -> list[str]:
    """Return every unsafe command found in fenced runbook content."""
    findings: list[str] = []
    in_fence = False
    for number, line in _logical_lines(text):
        command = line.strip()
        if command.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        if command.startswith("#"):
            continue
        # `pg_restore --list` with PGUSER=app_mei is legitimate read-only inspection;
        # connection identity is irrelevant, so this whole logical line is exempt.
        if "--list" in line:
            continue
        if PG_RESTORE.search(line) and "--role=app_migrator" not in line:
            findings.append(
                f"line {number}: pg_restore requires --role=app_migrator: {command}"
            )
        if CREATEDB.search(line) and "-O app_migrator" not in line:
            findings.append(
                f"line {number}: createdb requires -O app_migrator: {command}"
            )
        if "PGUSER=app_mei" in line:
            findings.append(f"line {number}: PGUSER=app_mei is forbidden: {command}")
    return findings


def test_restore_runbook_has_no_unsafe_restore_commands() -> None:
    text = (PROJECT_ROOT / "ops/RESTORE.md").read_text(encoding="utf-8")

    findings = restore_safety_findings(text)

    assert findings == [], (
        f"restore-safety guard found {len(findings)} violation(s):\n"
        + "\n".join(findings)
    )


def test_flags_wrapper_restore_without_role_and_app_user() -> None:
    """The positive control proves wrappers cannot hide either unsafe token."""
    # Given the stale production shape behind a wrapper and an environment assignment
    text = """\
```sh
docker compose exec -T -e PGUSER=app_mei db pg_restore --clean -d app_mei dump
```
"""

    # When the same text scanner used for the real runbook inspects it
    findings = restore_safety_findings(text)

    # Then it reports both independent hazards on their physical source line
    command = (
        "docker compose exec -T -e PGUSER=app_mei db pg_restore --clean -d app_mei dump"
    )
    assert findings == [
        f"line 2: pg_restore requires --role=app_migrator: {command}",
        f"line 2: PGUSER=app_mei is forbidden: {command}",
    ]


def test_line_continuations_report_the_first_physical_line() -> None:
    continuation = "\\"
    text = (
        "```sh\n"
        f"docker compose exec -T -e PGUSER=app_mei db {continuation}\n"
        "  pg_restore --clean -d app_mei dump\n"
        "```"
    )

    command = (
        "docker compose exec -T -e PGUSER=app_mei db pg_restore --clean -d app_mei dump"
    )
    assert restore_safety_findings(text) == [
        f"line 2: pg_restore requires --role=app_migrator: {command}",
        f"line 2: PGUSER=app_mei is forbidden: {command}",
    ]


def test_flags_createdb_without_migrator_owner() -> None:
    # Given a database creation command with PostgreSQL's default owner
    text = """\
```sh
createdb app_mei
```
"""

    # When the restore-safety scanner inspects the fenced command
    findings = restore_safety_findings(text)

    # Then the missing migrator ownership is reported
    assert findings == ["line 2: createdb requires -O app_migrator: createdb app_mei"]


def test_createdb_with_migrator_owner_is_safe() -> None:
    text = """\
```sh
createdb -O app_migrator app_mei
```
"""

    assert restore_safety_findings(text) == []


def test_list_inspection_is_exempt_even_with_app_user() -> None:
    text = """\
```sh
PGUSER=app_mei pg_restore --list backup.dump
```
"""

    assert restore_safety_findings(text) == []


def test_comment_only_fence_line_is_ignored() -> None:
    text = """\
```sh
# Do not run pg_restore without reviewing the ownership flags.
```
"""

    assert restore_safety_findings(text) == []


def test_restore_with_migrator_role_is_safe() -> None:
    text = """\
```sh
pg_restore --role=app_migrator --clean -d app_mei backup.dump
```
"""

    assert restore_safety_findings(text) == []


def test_prose_outside_fences_is_ignored() -> None:
    text = "Never run pg_restore without first checking the ownership procedure."

    assert restore_safety_findings(text) == []

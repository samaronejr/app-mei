"""Every unscoped `all_tenants()` call must be annotated, so a grep finds them all.

The escape hatch is deliberately unpleasant to use. Its whole value is that reviewing
cross-tenant access reduces to one grep, and that only holds if every call site is
annotated. This test is that grep, run in CI.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALL_MARKER = ".all_tenants("
APPROVAL_MARKER = "# ALL_TENANTS_OK:"

# This file quotes the pattern it searches for, so scanning itself would always match.
SELF = Path(__file__).resolve()
EXCLUDED_DIRECTORIES = {".venv", ".git", "__pycache__", ".mypy_cache", ".pytest_cache"}


def _python_sources() -> list[Path]:
    return [
        path
        for path in PROJECT_ROOT.rglob("*.py")
        if path.resolve() != SELF
        and not EXCLUDED_DIRECTORIES & set(path.relative_to(PROJECT_ROOT).parts)
    ]


def _unannotated_call_sites() -> list[str]:
    offenders = []
    for path in _python_sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if CALL_MARKER not in line:
                continue
            # The marker may sit on the call itself or on the line directly above it,
            # which is where a reason long enough to be useful naturally goes.
            window = [line, lines[number - 1] if number else ""]
            if not any(APPROVAL_MARKER in candidate for candidate in window):
                relative = path.relative_to(PROJECT_ROOT)
                offenders.append(f"{relative}:{number + 1}: {line.strip()}")
    return offenders


def test_the_scan_actually_reaches_the_codebase() -> None:
    # Given the project tree
    sources = _python_sources()

    # When it is enumerated
    # Then real files are found. Without this, a broken glob would make the guard
    # below pass by scanning nothing at all.
    assert len(sources) > 10
    assert any(path.name == "managers.py" for path in sources)


def test_the_scan_finds_at_least_one_real_call_site() -> None:
    # Given the project tree
    found = [
        f"{path.relative_to(PROJECT_ROOT)}:{number + 1}"
        for path in _python_sources()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if CALL_MARKER in line
    ]

    # When call sites are counted
    # Then there is at least one, so the guard is exercising a real pattern rather
    # than passing because nothing anywhere matches
    assert found, "no .all_tenants( call sites found — the guard would pass vacuously"


def test_every_unscoped_call_site_carries_an_approval_marker() -> None:
    # Given every Python source in the project except this file
    # When each `.all_tenants(` call is checked for an adjacent approval marker
    offenders = _unannotated_call_sites()

    # Then none is missing one
    assert not offenders, (
        "unscoped .all_tenants() calls without an adjacent "
        f"'{APPROVAL_MARKER} <reason>' comment:\n  " + "\n  ".join(offenders)
    )

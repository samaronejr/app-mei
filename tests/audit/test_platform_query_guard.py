"""Every application query on a policy-free table must be annotated or scoped.

For the six tables in `NON_TENANT_TABLES` the database enforces nothing, so the only
thing standing between a firm's data and another firm is that whoever wrote the query
remembered to filter it. A convention cannot be verified; this grep can.

The rule: any `<Model>.objects.…` in `apps/` either calls `.for_user(` or carries an
adjacent `# PLATFORM_QUERY_OK: <reason>` comment. The legitimate unscoped reads are
few and specific — the middleware's bootstrap lookup, the invite token lookup — and
each one having to state its reason is exactly the review surface that is wanted.
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = PROJECT_ROOT / "apps"

# The tables with no row-level-security policy. Listed literally rather than derived,
# so adding a seventh is a deliberate act that shows up in review.
PLATFORM_MODELS = (
    "Tenant",
    "Membership",
    "Invite",
    "AccessLog",
    "PlatformEvent",
    "DataSubjectRequest",
)
QUERY = re.compile(rf"\b({'|'.join(PLATFORM_MODELS)})\.objects\.")
SCOPED_CALL = ".for_user("
APPROVAL_MARKER = "# PLATFORM_QUERY_OK:"

# access.py IMPLEMENTS for_user, so its own query is the thing being verified rather
# than a caller of it. This file quotes the pattern and would always match itself.
EXCLUDED = {
    (APPS_ROOT / "core" / "access.py").resolve(),
    Path(__file__).resolve(),
}


def _application_sources() -> list[Path]:
    return [
        path
        for path in APPS_ROOT.rglob("*.py")
        if path.resolve() not in EXCLUDED
        and "migrations" not in path.parts
        and "__pycache__" not in path.parts
    ]


def _preceding_comment_block(lines: list[str], index: int) -> list[str]:
    """Return the contiguous comment lines immediately above `index`.

    The whole block, not just one line: a reason worth writing down rarely fits on
    one, and a guard that only checked the adjacent line would push authors towards
    reasons short enough to be useless.
    """
    block = []
    cursor = index - 1
    while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
        block.append(lines[cursor])
        cursor -= 1
    return block


def _offenders() -> list[str]:
    found = []
    for path in _application_sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if not QUERY.search(line):
                continue
            window = [line, *_preceding_comment_block(lines, number)]
            if SCOPED_CALL in line or any(APPROVAL_MARKER in c for c in window):
                continue
            found.append(
                f"{path.relative_to(PROJECT_ROOT)}:{number + 1}: {line.strip()}"
            )
    return found


def test_the_scan_actually_reaches_the_application_code() -> None:
    # Given the apps tree
    sources = _application_sources()

    # When it is enumerated
    # Then real modules are found. A broken glob would make the guard below pass by
    # scanning nothing at all.
    assert len(sources) > 10
    assert any(path.name == "middleware.py" for path in sources)


def test_the_scan_finds_at_least_one_real_query() -> None:
    # Given the apps tree
    matches = [
        f"{path.relative_to(PROJECT_ROOT)}:{number + 1}"
        for path in _application_sources()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if QUERY.search(line)
    ]

    # When queries on policy-free tables are counted
    # Then there is at least one, so the guard exercises a real pattern
    assert matches, "no platform-table queries found — the guard would pass vacuously"


def test_every_platform_table_query_is_scoped_or_justified() -> None:
    # Given every application module
    # When each query on a policy-free table is checked
    offenders = _offenders()

    # Then none is both unscoped and unexplained
    assert not offenders, (
        "queries on tables with NO database-layer isolation that neither use "
        f"for_user() nor carry an adjacent '{APPROVAL_MARKER} <reason>' comment:\n  "
        + "\n  ".join(offenders)
    )

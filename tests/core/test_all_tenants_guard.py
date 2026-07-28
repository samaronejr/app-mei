"""Every unscoped read must be annotated, so a grep finds all of them.

There are two ways to leave the tenant filter behind and, until now, only one was
guarded. `.all_tenants()` is the explicit escape hatch on the scoped manager.
`.all_objects` is its silent twin: `TenantScopedModel` declares it so the scoped
manager never becomes Django's `_base_manager`, which has the side effect of giving
every tenant-scoped model a completely unfiltered manager as a public attribute.
Reaching for it skips the ORM's ContextVar filter exactly as `.all_tenants()` does,
with none of the vocabulary that announces the intent — which is precisely why it is
the more dangerous of the two.

Both are legitimate in the right place and unreviewable in the wrong one. The whole
value of the convention is that auditing unscoped access reduces to one grep, and
that only holds if every call site is annotated. This test is that grep, run in CI.

Each hatch carries its own approval marker rather than sharing one, because they
answer different questions: `# ALL_TENANTS_OK:` marks a deliberate read ACROSS
tenants, `# ALL_OBJECTS_OK:` marks a read that bypasses the application-layer filter
and leans on row-level security alone. A reviewer auditing one should not have to
read the other's call sites to find out which kind they are looking at.

`all_objects` is matched with its leading dot, so the guard catches attribute ACCESS
and not the manager's declaration on `TenantScopedModel`, nor the `managers=[...]`
entry Django writes into a migration for it. Those are the same word describing the
manager's existence rather than issuing a query, and demanding a reason on each would
turn the convention into noise — which is the reliable way to get a guard deleted
rather than obeyed.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Hatch:
    """One way out of the tenant filter, and the comment that approves taking it."""

    call: str
    approval: str
    exempt: tuple[str, ...] = field(default=())

    def __str__(self) -> str:
        """Name the hatch by its call marker, so a failure reads as the grep it is."""
        return self.call


HATCHES: Final[tuple[Hatch, ...]] = (
    Hatch(".all_tenants(", "# ALL_TENANTS_OK:"),
    # apps/core is where TenantScopedModel, its Meta and its managers live, so a
    # reference to the unscoped manager inside it is the declaration being guarded
    # rather than a use of it.
    Hatch(".all_objects", "# ALL_OBJECTS_OK:", exempt=("apps/core",)),
)

# This file quotes every pattern it searches for, so scanning itself would always match.
SELF = Path(__file__).resolve()
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def _python_sources() -> list[Path]:
    return [
        path
        for path in PROJECT_ROOT.rglob("*.py")
        if path.resolve() != SELF
        and not EXCLUDED_DIRECTORIES & set(path.relative_to(PROJECT_ROOT).parts)
    ]


def _sources_for(hatch: Hatch) -> list[Path]:
    return [
        path
        for path in _python_sources()
        if not any(
            path.relative_to(PROJECT_ROOT).as_posix().startswith(f"{prefix}/")
            for prefix in hatch.exempt
        )
    ]


def _call_sites(hatch: Hatch) -> list[str]:
    return [
        f"{path.relative_to(PROJECT_ROOT)}:{number + 1}"
        for path in _sources_for(hatch)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if hatch.call in line
    ]


def _preceding_comment_block(lines: list[str], index: int) -> list[str]:
    """Return the contiguous comment lines immediately above `index`.

    The whole block, not just the adjacent line: a reason worth writing down rarely
    fits on one, and a guard that checked only the line above would push authors
    towards reasons short enough to be useless. This matches how the sibling
    platform-query guard reads its own approvals.
    """
    block = []
    cursor = index - 1
    while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
        block.append(lines[cursor])
        cursor -= 1
    return block


def _unannotated_call_sites(hatch: Hatch) -> list[str]:
    offenders = []
    for path in _sources_for(hatch):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if hatch.call not in line:
                continue
            window = [line, *_preceding_comment_block(lines, number)]
            if not any(hatch.approval in candidate for candidate in window):
                relative = path.relative_to(PROJECT_ROOT)
                offenders.append(f"{relative}:{number + 1}: {line.strip()}")
    return offenders


def test_the_scan_actually_reaches_the_codebase() -> None:
    # Given the project tree
    sources = _python_sources()

    # When it is enumerated
    # Then real files are found. Without this, a broken glob would make every guard
    # below pass by scanning nothing at all.
    assert len(sources) > 10
    assert any(path.name == "managers.py" for path in sources)


@pytest.mark.parametrize("hatch", HATCHES, ids=str)
def test_the_exemption_does_not_swallow_the_scan(hatch: Hatch) -> None:
    # Given a hatch that exempts part of the tree
    scanned = _sources_for(hatch)

    # When the exemption is applied
    # Then most of the tree survives it. An exemption broad enough to empty the scan
    # would leave the guard passing while checking nothing.
    assert len(scanned) > len(_python_sources()) // 2


@pytest.mark.parametrize("hatch", HATCHES, ids=str)
def test_the_scan_finds_at_least_one_real_call_site(hatch: Hatch) -> None:
    # Given the project tree
    # When call sites are counted
    found = _call_sites(hatch)

    # Then there is at least one, so the guard is exercising a real pattern rather
    # than passing because nothing anywhere matches
    assert found, f"no {hatch.call} call sites found — the guard would pass vacuously"


@pytest.mark.parametrize("hatch", HATCHES, ids=str)
def test_every_unscoped_call_site_carries_an_approval_marker(hatch: Hatch) -> None:
    # Given every Python source in the project except this file
    # When each call is checked for an adjacent approval marker
    offenders = _unannotated_call_sites(hatch)

    # Then none is missing one
    assert not offenders, (
        f"unscoped {hatch.call} calls without an adjacent "
        f"'{hatch.approval} <reason>' comment:\n  " + "\n  ".join(offenders)
    )

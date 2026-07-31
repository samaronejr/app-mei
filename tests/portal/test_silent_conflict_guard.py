"""No portal write path resolves a conflict silently.

W8. Three of the four existence oracles a write grant creates announce themselves: a
unique violation raises `23505`, a foreign key violation raises `23503`, and a policy
violation names the policy. `ON CONFLICT DO NOTHING` is the one that does not — it
returns `INSERT 0 0` where a successful write returns `INSERT 0 1`, with no error
at all.

That difference is a one-bit answer about a row the caller cannot read, delivered
through a code path that looks like ordinary defensive programming. `get_or_create` and
`bulk_create(ignore_conflicts=True)` are the ORM spellings of the same thing.

**This guard currently finds nothing, and that is precisely why the self-test below
exists.** No portal write path is written yet — the vault views are Stage 5 — so a guard
without proof that its pattern can fire would pass today, pass when Stage 5 lands, and
keep passing if the pattern were silently broken. The existing client-id provenance
guard carries the same pair for the same reason, and its docstring records that an
earlier version of it matched nothing at all, in any file.
"""

import re
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PORTAL_WRITE_ROOTS: Final[tuple[Path, ...]] = (PROJECT_ROOT / "apps" / "portal",)

SILENT_CONFLICT: Final = re.compile(
    r"\bON\s+CONFLICT\b"
    r"|\bignore_conflicts\s*="
    r"|\.get_or_create\("
    r"|\.aget_or_create\(",
    re.IGNORECASE,
)

# Every spelling the guard must catch, kept beside the pattern so the two cannot drift.
VIOLATIONS: Final[tuple[str, ...]] = (
    'cursor.execute("INSERT INTO obligations_document ... ON CONFLICT DO NOTHING")',
    "Document.objects.bulk_create(rows, ignore_conflicts=True)",
    "Document.objects.get_or_create(storage_key=key, defaults=fields)",
    "await Document.objects.aget_or_create(storage_key=key)",
)

# Text that must NOT trip it, so the pattern is not merely "matches everything".
INNOCENT: Final[tuple[str, ...]] = (
    "Document.objects.create(**fields)",
    "conflict = resolve(a, b)",
    "update_or_create is legitimate in a data migration",
)


def _without_comments(source: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def _sources() -> list[Path]:
    return [
        path
        for root in PORTAL_WRITE_ROOTS
        for path in root.rglob("*.py")
        # Migrations are excluded for the same reason the provenance guard excludes
        # them: `update_or_create` is how a data migration stays replayable, and a
        # migration has no untrusted caller to hide a conflict from.
        if "__pycache__" not in path.parts and "migrations" not in path.parts
    ]


def test_the_scan_reaches_real_modules() -> None:
    # Given the portal tree
    sources = _sources()

    # Then it is not empty. A guard whose glob matches nothing passes every assertion
    # beneath it, which is the quietest way for one of these to stop working.
    assert sources, f"no sources under {[str(r) for r in PORTAL_WRITE_ROOTS]}"
    assert any(path.name == "middleware.py" for path in sources)


def test_the_pattern_catches_every_spelling_it_is_meant_to() -> None:
    # Given each way of resolving a conflict silently
    for violation in VIOLATIONS:
        # Then the pattern sees it. Without this the guard could ship with a literal
        # that matches nothing in any file and report a clean tree forever.
        assert SILENT_CONFLICT.search(violation), violation


def test_the_pattern_does_not_match_ordinary_code() -> None:
    # Given writes and words that are not conflict suppression
    for innocent in INNOCENT:
        # Then it stays quiet, so a clean report means something
        assert not SILENT_CONFLICT.search(innocent), innocent


def test_no_portal_write_path_resolves_a_conflict_silently() -> None:
    # Given every portal module
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}"
        for path in _sources()
        for number, line in enumerate(
            _without_comments(path.read_text(encoding="utf-8")).splitlines(),
            start=1,
        )
        if SILENT_CONFLICT.search(line)
    ]

    # Then none suppresses a conflict. INSERT 0 0 versus INSERT 0 1 is a one-bit answer
    # about a row the caller cannot read, and unlike every other oracle here it arrives
    # with no error to notice.
    assert not offenders, offenders

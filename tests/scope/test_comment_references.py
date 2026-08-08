"""Every `tests/…py` path named under `apps/**` must be a file that exists.

A comment citing a test module is a claim about where a rule is proved, and it is
exactly the kind of claim that rots in silence. Renaming or deleting a test leaves the
sentence pointing at nothing, and the next reader either trusts a guarantee nobody is
holding or spends an afternoon looking for the file. `apps/portal/invite_views.py`
cited `tests/ui/test_portal_invite_walkthrough.py` from the day the portal shipped;
that module has never existed. Nothing went red, because nothing was watching. This is
what watches.

Scoped to `apps/**` because that is where the rules being cited live. Two exclusions:

* `migrations`, because a migration is a frozen historical artefact. A citation that
  went stale inside one cannot be repaired without editing a file that has already run
  against real data, so a red here would be a permanent red demanding a forbidden fix.
  Every citation currently inside a migration resolves anyway.
* `__pycache__`, following every scan in `tests/scope/test_scope_fidelity.py`. It is
  DEFENSIVE rather than load-bearing here and the difference is worth stating: that
  directory holds `.pyc` files, which a `*.py` walk never reaches, so the filter removes
  nothing today. It is kept so that widening the suffix set later cannot quietly start
  pinning machine-specific bytecode paths.

Raw lines rather than parsed comment tokens, following the scope suite's own `_grep`
convention. That is the stricter reading, not the lazier one: every path named in a
comment or a docstring is caught, and so is one written into a string literal — which is
a claim about a file just as much, and just as able to rot.
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCANNED_ROOTS: Final[tuple[str, ...]] = ("apps",)
EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({"__pycache__", "migrations"})

# Anchored on the `tests/` root and stopped at `.py`, so a citation carrying a line
# range (`tests/ui/test_clients_pages.py:127-135` is the shipped shape) yields the path
# alone.
TEST_PATH: Final = re.compile(r"tests/[A-Za-z0-9_./-]*\.py")

# The planted positive's two operands. The real one is this module, so the control
# cannot start passing because the tree stopped having test files in it.
REAL_PATH: Final = "tests/scope/test_comment_references.py"
INVENTED_PATH: Final = "tests/scope/test_nao_existe_este_modulo.py"


def _sources(excluded: frozenset[str] = EXCLUDED_PARTS) -> list[Path]:
    """Return the Python sources under the scanned roots, minus the excluded parts."""
    return sorted(
        path
        for root in SCANNED_ROOTS
        for path in (PROJECT_ROOT / root).rglob("*.py")
        if path.is_file() and not (excluded & set(path.parts))
    )


def _references(sources: Sequence[Path]) -> list[tuple[str, str]]:
    """Return `(location, cited path)` for every `tests/…py` path named in `sources`."""
    found: list[tuple[str, str]] = []
    for path in sources:
        location = path.relative_to(PROJECT_ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            found.extend(
                (f"{location}:{number}", match.group(0))
                for match in TEST_PATH.finditer(line)
            )
    return found


def _dangling(references: Iterable[tuple[str, str]]) -> list[str]:
    """Return `location: cited path` for every citation that names no file on disk."""
    return sorted(
        f"{location}: {cited}"
        for location, cited in references
        if not (PROJECT_ROOT / cited).is_file()
    )


def _report(label: str, hits: Sequence[str]) -> str:
    """Format offenders one per line, because a bare `assert []` names nothing."""
    return f"{label} — {len(hits)} offending citation(s):\n" + "\n".join(hits)


def test_every_test_path_named_under_apps_exists() -> None:
    # Given every application source, migrations and bytecode aside
    sources = _sources()
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When each `tests/…py` path they name is collected
    references = _references(sources)
    assert references, (
        "no tests/… path was found under apps/**, so this guard has nothing to check "
        "and would pass just as happily against a tree where every citation had been "
        "deleted"
    )

    # Then each one names a file that is really there. A citation is a promise about
    # where a rule is proved; a dangling one is a promise with no proof behind it.
    strays = _dangling(references)
    assert strays == [], _report(
        "citation names a test file that does not exist", strays
    )


def test_the_scan_reports_a_citation_that_names_nothing() -> None:
    """The planted positive: a detector finding nothing passes the case above free."""
    # Given one citation that resolves and one that cannot
    assert (PROJECT_ROOT / REAL_PATH).is_file(), REAL_PATH
    assert not (PROJECT_ROOT / INVENTED_PATH).is_file(), INVENTED_PATH

    # When both are put through the same check the guard uses
    reported = _dangling(
        [("apps/plantado.py:1", REAL_PATH), ("apps/plantado.py:2", INVENTED_PATH)],
    )

    # Then exactly the unresolvable one is named, at its location
    assert reported == [f"apps/plantado.py:2: {INVENTED_PATH}"], reported


def test_the_regex_reads_a_citation_out_of_the_prose_around_it() -> None:
    # Given the three shapes citations are actually written in this tree: bare, wrapped
    # in backticks, and carrying a line range
    line = (
        "    membership manager rather than this one — see `tests/a/test_b.py` and "
        "tests/c/test_d.py:127-135 for the pair."
    )

    # When the pattern is run over it
    # Then both paths come out without the punctuation or the range attached. A pattern
    # that swallowed the backtick or the `:127-135` would report every citation as
    # dangling and the guard above would be unfixable rather than green.
    assert TEST_PATH.findall(line) == ["tests/a/test_b.py", "tests/c/test_d.py"]


def test_the_scan_excludes_only_what_its_docstring_claims() -> None:
    # Given the scanned set and the same walk with nothing subtracted
    scanned = _sources()
    unfiltered = _sources(frozenset())

    # Then the exclusion removed something, so it is a real subtraction rather than
    # machinery that reads as rigour
    assert len(unfiltered) > len(scanned), (
        "the exclusion removed no file at all, so the two sets are the same scan"
    )

    # …and what it removed is migrations and nothing else
    removed = set(unfiltered) - set(scanned)
    assert all("migrations" in path.parts for path in removed), sorted(
        str(path.relative_to(PROJECT_ROOT))
        for path in removed
        if "migrations" not in path.parts
    )
    assert not any(EXCLUDED_PARTS & set(path.parts) for path in scanned)

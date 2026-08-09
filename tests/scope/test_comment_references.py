"""Require cited test modules under the selected source roots to resolve.

A comment citing a test module is a claim about where a rule is proved, and it is
exactly the kind of claim that rots in silence. Renaming or deleting a test leaves the
sentence pointing at nothing, and the next reader either trusts a guarantee nobody is
holding or spends an afternoon looking for the file. A portal-invite comment once cited
a walkthrough test module that had never existed. Nothing went red, because nothing was
watching. This is what watches.

The required roots are `apps`, `config`, `templates`, and `tests`. Documentation is
included separately because every path-shaped citation measured under `docs` resolves.
The scan reads Python, HTML, and Markdown sources. Two directory exclusions remain:

* `migrations` is load-bearing. A migration is a frozen historical artefact, so a stale
  citation inside one cannot be repaired without editing a file that has already run
  against real data. The subtraction test proves migrations are really removed.
* `__pycache__` is defensive. Its `.pyc` files are already outside the selected
  suffixes, but naming the exclusion prevents a future suffix widening from silently
  pinning machine-specific bytecode paths.

Path-shaped citations are read from raw source lines. That catches paths in comments,
docstrings, and string literals. Dotted test-module names are checked separately and
only in prose: Python comments and docstrings, plus HTML and Markdown source. Executable
Python imports are deliberately outside that check because the interpreter already
validates them. Dotted names resolve deterministically to a module file or package.

This guard proves only that a cited test module exists and resolves. It cannot prove
that the cited test still proves what the surrounding comment claims: repointing a
comment at a real but wrong file passes. Semantic drift is outside this check.
"""

import ast
import io
import re
import tokenize
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCANNED_ROOTS: Final[tuple[str, ...]] = ("apps", "config", "templates", "tests")
DOCUMENTATION_ROOTS: Final[tuple[str, ...]] = ("docs",)
SCANNED_SUFFIXES: Final[frozenset[str]] = frozenset({".html", ".md", ".py"})
EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({"__pycache__", "migrations"})

# Anchored on the `tests/` root and stopped at `.py`, so a citation carrying a line
# range (`tests/ui/test_clients_pages.py:127-135` is the shipped shape) yields the path
# alone.
TEST_PATH: Final = re.compile(r"tests/[A-Za-z0-9_./-]*\.py")
DOTTED_TEST_MODULE: Final = re.compile(
    r"(?<![A-Za-z0-9_.])tests(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}"
    r"(?![A-Za-z0-9_])"
)

# The planted positive's two operands. The real one is this module, so the control
# cannot start passing because the tree stopped having test files in it.
REAL_PATH: Final = "tests/scope/test_comment_references.py"
INVENTED_PATH: Final = (
    Path("tests") / "scope" / "test_nao_existe_este_modulo.py"
).as_posix()


def _sources(excluded: frozenset[str] = EXCLUDED_PARTS) -> list[Path]:
    """Return text sources under the scanned roots, minus the excluded parts."""
    return sorted(
        path
        for root in SCANNED_ROOTS + DOCUMENTATION_ROOTS
        for path in (PROJECT_ROOT / root).rglob("*")
        if path.is_file()
        and path.suffix in SCANNED_SUFFIXES
        and not (excluded & set(path.parts))
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


def _prose_lines(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if path.suffix != ".py":
        return list(enumerate(lines, 1))

    found = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT
    ]
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for owner in ast.walk(ast.parse(text)):
        if not isinstance(owner, owners) or not owner.body:
            continue
        statement = owner.body[0]
        if not isinstance(statement, ast.Expr):
            continue
        value = statement.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        end_line = value.end_lineno or value.lineno
        found.extend(
            (number, lines[number - 1]) for number in range(value.lineno, end_line + 1)
        )
    return sorted(set(found))


def _dotted_references(sources: Sequence[Path]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sources:
        location = path.relative_to(PROJECT_ROOT)
        for number, line in _prose_lines(path):
            found.extend(
                (f"{location}:{number}", match.group(0))
                for match in DOTTED_TEST_MODULE.finditer(line)
            )
    return found


def _dotted_module_exists(cited: str) -> bool:
    target = PROJECT_ROOT.joinpath(*cited.split("."))
    return target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()


def _dangling_dotted(references: Iterable[tuple[str, str]]) -> list[str]:
    return sorted(
        f"{location}: {cited}"
        for location, cited in references
        if not _dotted_module_exists(cited)
    )


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


def test_the_required_roots_are_scanned() -> None:
    assert SCANNED_ROOTS == ("apps", "config", "templates", "tests")


def test_every_test_path_named_under_the_scanned_roots_exists() -> None:
    # Given every selected source, migrations and bytecode aside
    sources = _sources()
    assert len(sources) > 100, "the scan found nothing and would pass vacuously"

    # When each `tests/…py` path they name is collected
    references = _references(sources)
    assert references, (
        "no tests/… path was found under the selected roots, so this guard has nothing "
        "to check "
        "and would pass just as happily against a tree where every citation had been "
        "deleted"
    )
    for root in SCANNED_ROOTS[1:] + DOCUMENTATION_ROOTS:
        root_hits = sum(location.startswith(f"{root}/") for location, _ in references)
        assert root_hits >= 1, f"no tests/… path found under newly scanned root {root}/"

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
    first = (Path("tests") / "a" / "test_b.py").as_posix()
    second = (Path("tests") / "c" / "test_d.py").as_posix()
    line = f"membership manager — see `{first}` and {second}:127-135 for the pair."

    # When the pattern is run over it
    # Then both paths come out without the punctuation or the range attached. A pattern
    # that swallowed the backtick or the `:127-135` would report every citation as
    # dangling and the guard above would be unfixable rather than green.
    assert TEST_PATH.findall(line) == [first, second]


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


def test_every_dotted_test_module_named_in_prose_resolves() -> None:
    import_only_source = PROJECT_ROOT / "tests/authz/test_portfolio.py"
    assert DOTTED_TEST_MODULE.search(import_only_source.read_text(encoding="utf-8"))
    assert _dotted_references([import_only_source]) == []

    references = _dotted_references(_sources())
    assert references, "the prose-only dotted-module scan found nothing"

    strays = _dangling_dotted(references)
    assert strays == [], _report("dotted citation does not resolve", strays)

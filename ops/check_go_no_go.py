"""Decide go/no-go for the controlled pilot from the ledger and the evidence directory.

Usage: check_go_no_go.py <ledger> <evidence-dir>

Run from a checkout of the repository: the final-release rule resolves
`origin/main` with git in the current working directory.

The ledger (ops/PILOT-GO-NO-GO.md) holds one table row per Success-criteria
line S1-S21, plus optional continuation rows whose first cell is empty. Each
row names one evidence file and the literals that file must carry. A literal
is present when some line of the file starts with it and the next character
is the end of the line, whitespace, or `;`. A literal that ends in `=` only
requires the key; its value is checked by the final-release rule below.

Every S-line is NO-GO when any of its rows fails:

- the evidence file is absent, or lacks an expected literal;
- the ledger structure is wrong: an S-line is missing or duplicated, or the
  kinds break the plan's split (S1, S2, S3 and S20 are final-release, S18 has a
  final-release part, every other line is drill-only);
- for final-release rows, the final capture in PILOT2-506-final.txt does not
  name one SHA: `final_main_sha`, the unpinned and Lightsail `/versionz`
  releases and the last CI and verify-live head SHAs must all equal
  `git rev-parse origin/main` at check time; `capture_utc` must not predate
  that commit; and no value in the capture may read PENDING or UNVERIFIED.

Drill rows are proven at the SHA they ran. That SHA (head_sha= or drill_sha=)
is printed as historical and never compared with the final release.

Exits 0 when all 21 lines are GO, 1 naming every NO-GO line, 2 on unreadable
input. Stdlib only, so it runs under any system python3.
"""

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

FINAL_CAPTURE: Final = "PILOT2-506-final.txt"
S_LINES: Final = tuple(f"S{number}" for number in range(1, 22))
FINAL_PRIMARY: Final = frozenset({"S1", "S2", "S3", "S20"})
FINAL_ALLOWED: Final = FINAL_PRIMARY | {"S18"}
FINAL_SHA_KEYS: Final = (
    "final_main_sha",
    "unpinned_versionz",
    "lightsail_versionz",
    "ci_push_head_sha",
    "verify_live_head_sha",
)
KINDS: Final = frozenset({"final-release", "drill"})
HEADER: Final = ("line", "kind", "evidence_file", "expected", "rule")

LITERAL_BOUNDARY: Final = frozenset(" \t;")
BACKTICKED: Final = re.compile(r"`([^`]+)`")
CELL_SPLIT: Final = re.compile(r"(?<!\\)\|")
EVIDENCE_NAME: Final = re.compile(r"PILOT2?-[A-Za-z0-9._-]+\.(?:txt|md)")
HISTORICAL_SHA: Final = re.compile(
    r"^(head_sha|drill_sha)=([0-9a-f]{40})\b", re.MULTILINE
)
REFUSED_VALUE: Final = re.compile(r"PENDING|UNVERIFIED", re.IGNORECASE)
SHA: Final = re.compile(r"[0-9a-f]{40}")
S_ID: Final = re.compile(r"S[0-9]+")
SEPARATOR_CELL: Final = re.compile(r":?-+:?")
UTC_STAMP: Final = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Row:
    """One ledger table row: an evidence file and the literals it must carry."""

    s_line: str
    kind: str
    evidence: str
    literals: tuple[str, ...]


class LedgerError(Exception):
    """The ledger has no parsable S table."""


def _cells(line: str) -> list[str]:
    parts = CELL_SPLIT.split(line.strip())
    return [part.strip().replace("\\|", "|") for part in parts[1:-1]]


def parse_ledger(text: str) -> tuple[list[Row], dict[str, list[str]]]:
    """Return the S table's rows and any per-line structural problems."""
    lines = text.splitlines()
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("|") and tuple(_cells(line)) == HEADER
        )
    except StopIteration:
        message = (
            "no table with header | line | kind | evidence_file | expected | rule |"
        )
        raise LedgerError(message) from None

    rows: list[Row] = []
    primaries: list[str] = []
    problems: dict[str, list[str]] = {}
    current = ""
    for number, line in enumerate(lines[start + 1 :], start + 2):
        if not line.startswith("|"):
            break
        cells = _cells(line)
        if all(SEPARATOR_CELL.fullmatch(cell) for cell in cells):
            continue
        s_cell = cells[0] if cells else ""
        if S_ID.fullmatch(s_cell):
            current = s_cell
            primaries.append(s_cell)
        elif s_cell or not current:
            message = f"ledger line {number}: row has no S-line"
            raise LedgerError(message)
        row, found = _row(number, cells, current)
        problems.setdefault(current, []).extend(found)
        if row is not None:
            rows.append(row)
    _structure_problems(rows, primaries, problems)
    return rows, problems


def _row(number: int, cells: list[str], s_line: str) -> tuple[Row | None, list[str]]:
    """Build one Row from its cells, or report why the cells cannot be read."""
    where = f"ledger line {number}"
    if len(cells) != len(HEADER):
        return None, [f"{where}: expected {len(HEADER)} cells, got {len(cells)}"]
    kind, evidence_cell, expected = cells[1], cells[2], cells[3]
    evidence = BACKTICKED.findall(evidence_cell)
    literals = tuple(BACKTICKED.findall(expected))
    problems: list[str] = []
    if kind not in KINDS:
        problems.append(f"{where}: unknown kind {kind!r}")
    if not literals:
        problems.append(f"{where}: no expected literal")
    if len(evidence) != 1 or not EVIDENCE_NAME.fullmatch(evidence[0]):
        problems.append(f"{where}: evidence_file must be one PILOT-* file name")
        return None, problems
    return Row(s_line, kind, evidence[0], literals), problems


def _structure_problems(
    rows: list[Row], primaries: list[str], problems: dict[str, list[str]]
) -> None:
    for s_line in S_LINES:
        count = primaries.count(s_line)
        if count == 0:
            problems.setdefault(s_line, []).append("no ledger row")
        elif count > 1:
            problems.setdefault(s_line, []).append(f"{count} ledger rows, expected 1")
    for s_line in sorted(set(primaries) - set(S_LINES)):
        problems.setdefault(s_line, []).append("not a Success-criteria line")

    by_line: dict[str, list[Row]] = {}
    for row in rows:
        by_line.setdefault(row.s_line, []).append(row)
    for s_line, line_rows in by_line.items():
        kinds = [row.kind for row in line_rows]
        if s_line in FINAL_PRIMARY and kinds[0] != "final-release":
            problems.setdefault(s_line, []).append("first row must be final-release")
        if s_line == "S18" and "final-release" not in kinds:
            problems.setdefault(s_line, []).append("needs a final-release row")
        if s_line not in FINAL_ALLOWED and "final-release" in kinds:
            problems.setdefault(s_line, []).append("drill line has a final-release row")
        problems.setdefault(s_line, []).extend(
            f"final-release row must read {FINAL_CAPTURE}, not {row.evidence}"
            for row in line_rows
            if row.kind == "final-release" and row.evidence != FINAL_CAPTURE
        )


def has_literal(lines: list[str], literal: str) -> bool:
    """Return whether a line starts with `literal` at a token boundary."""
    for line in lines:
        if not line.startswith(literal):
            continue
        rest = line[len(literal) :]
        if literal.endswith("=") or not rest or rest[0] in LITERAL_BOUNDARY:
            return True
    return False


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    result = subprocess.run(  # noqa: S603
        [git, *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def final_capture_problems(text: str) -> list[str]:
    """Return every way the final capture fails to name one released SHA."""
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values.setdefault(key.strip(), []).append(value.strip())

    problems = [
        f"{key}={value} is not an observation"
        for key, entries in values.items()
        for value in entries
        if REFUSED_VALUE.search(value)
    ]
    problems += _sha_problems(values)
    problems += _capture_time_problems(values.get("capture_utc", []))
    return problems


def _sha_problems(values: dict[str, list[str]]) -> list[str]:
    """Every SHA key once, 40-hex, equal to final_main_sha and to origin/main."""
    problems: list[str] = []
    shas: dict[str, str] = {}
    for key in FINAL_SHA_KEYS:
        entries = values.get(key, [])
        if len(entries) != 1:
            problems.append(f"{key} appears {len(entries)} times, expected 1")
        elif not SHA.fullmatch(entries[0]):
            problems.append(f"{key}={entries[0]} is not a 40-hex SHA")
        else:
            shas[key] = entries[0]

    final = shas.get("final_main_sha")
    problems.extend(
        f"{key} ({value}) != final_main_sha ({final})"
        for key, value in shas.items()
        if final is not None and value != final
    )
    origin_main = _git("rev-parse", "--verify", "origin/main^{commit}")
    if origin_main is None:
        problems.append("git rev-parse origin/main failed in the current directory")
    elif final is not None and final != origin_main:
        problems.append(
            f"final_main_sha ({final}) != git rev-parse origin/main ({origin_main})"
        )
    return problems


def _capture_time_problems(stamps: list[str]) -> list[str]:
    """One capture_utc, in UTC_STAMP form, no earlier than the origin/main commit."""
    if len(stamps) != 1:
        return [f"capture_utc appears {len(stamps)} times, expected 1"]
    try:
        captured = datetime.strptime(stamps[0], UTC_STAMP).replace(tzinfo=UTC)
    except ValueError:
        return [f"capture_utc={stamps[0]} is not {UTC_STAMP}"]
    commit_epoch = _git("show", "-s", "--format=%ct", "origin/main")
    if commit_epoch is None:
        return ["git show origin/main failed in the current directory"]
    if captured < datetime.fromtimestamp(int(commit_epoch), tz=UTC):
        return [f"capture_utc={stamps[0]} predates the origin/main commit"]
    return []


def check(rows: list[Row], evidence_dir: Path) -> dict[str, list[str]]:
    """Return the evidence problems per S-line."""
    problems: dict[str, list[str]] = {}
    texts: dict[str, str | None] = {}
    for row in rows:
        if row.evidence not in texts:
            path = evidence_dir / row.evidence
            texts[row.evidence] = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.is_file()
                else None
            )
        text = texts[row.evidence]
        if text is None:
            problems.setdefault(row.s_line, []).append(
                f"evidence file absent: {row.evidence}"
            )
            continue
        lines = [line.rstrip() for line in text.splitlines()]
        problems.setdefault(row.s_line, []).extend(
            f"{row.evidence} lacks `{literal}`"
            for literal in row.literals
            if not has_literal(lines, literal)
        )

    final_lines = sorted(
        {row.s_line for row in rows if row.kind == "final-release"},
        key=lambda s_line: int(s_line[1:]),
    )
    final_text = texts.get(FINAL_CAPTURE)
    if final_lines and final_text is not None:
        for problem in final_capture_problems(final_text):
            for s_line in final_lines:
                problems.setdefault(s_line, []).append(problem)
    return problems


def _historical(rows: list[Row], s_line: str, evidence_dir: Path) -> str:
    notes: list[str] = []
    for row in rows:
        if row.s_line != s_line or row.kind != "drill":
            continue
        text = (evidence_dir / row.evidence).read_text(
            encoding="utf-8", errors="replace"
        )
        match = HISTORICAL_SHA.search(text)
        sha = f"{match.group(1)}={match.group(2)}" if match else "no head_sha recorded"
        notes.append(f"{row.evidence} {sha}")
    return "; ".join(dict.fromkeys(notes))


def main(argv: list[str] | None = None) -> int:
    """Print one verdict per S-line; return 0 when all 21 are GO."""
    parser = argparse.ArgumentParser(
        description="Check the pilot go/no-go ledger against the evidence directory."
    )
    parser.add_argument("ledger", type=Path, help="ops/PILOT-GO-NO-GO.md")
    parser.add_argument("evidence_dir", type=Path, help="the $EVR directory")
    args = parser.parse_args(argv)

    if not args.evidence_dir.is_dir():
        sys.stderr.write(f"check_go_no_go: not a directory: {args.evidence_dir}\n")
        return 2
    try:
        rows, problems = parse_ledger(args.ledger.read_text(encoding="utf-8"))
    except (OSError, LedgerError) as exc:
        sys.stderr.write(f"check_go_no_go: {exc}\n")
        return 2

    for s_line, found in check(rows, args.evidence_dir).items():
        problems.setdefault(s_line, []).extend(found)

    reported = sorted(set(S_LINES) | set(problems), key=lambda s: int(s[1:]))
    failing = [s_line for s_line in reported if problems.get(s_line)]
    for s_line in reported:
        if problems.get(s_line):
            for problem in problems[s_line]:
                sys.stderr.write(f"NO-GO {s_line}: {problem}\n")
            continue
        kinds = {row.kind for row in rows if row.s_line == s_line}
        history = _historical(rows, s_line, args.evidence_dir)
        detail = f" historical: {history}" if history else ""
        sys.stdout.write(f"GO {s_line} ({'+'.join(sorted(kinds))}){detail}\n")

    if failing:
        sys.stderr.write(f"NO-GO: {len(failing)} line(s) fail: {', '.join(failing)}\n")
        return 1
    sys.stdout.write(f"GO {len(S_LINES)}/{len(S_LINES)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Exercise ops/check_go_no_go.py, the pilot go/no-go verdict over $EVR.

Each test builds an evidence directory and a throwaway git repository whose
`origin/main` is a known commit, then runs the checker the way the operator does:
as a subprocess, from inside the checkout.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = PROJECT_ROOT / "ops" / "check_go_no_go.py"
REAL_LEDGER: Final = PROJECT_ROOT / "ops" / "PILOT-GO-NO-GO.md"
FIXTURE_LEDGER: Final = (
    PROJECT_ROOT / "tests" / "ops" / "fixtures" / "go_no_go_ledger.md"
)
FINAL: Final = "PILOT2-506-final.txt"
DRILL: Final = "PILOT2-fixture-drill.txt"
COMMIT_DATE: Final = "2030-01-01T12:00:00+00:00"
AFTER_COMMIT: Final = "2030-01-01T12:30:00Z"
BEFORE_COMMIT: Final = "2030-01-01T11:59:59Z"
HISTORICAL_SHA: Final = "a" * 40
OTHER_SHA: Final = "b" * 40
FINAL_RELEASE_LINES: Final = ("S1", "S2", "S3", "S18", "S20")
GIT_ENV: Final = {
    **os.environ,
    "GIT_AUTHOR_DATE": COMMIT_DATE,
    "GIT_COMMITTER_DATE": COMMIT_DATE,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None, "git is required to build the origin/main fixture"
    result = subprocess.run(  # noqa: S603
        [git, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=GIT_ENV,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose origin/main is one commit made at COMMIT_DATE."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(
        path,
        "-c",
        "user.name=ledger",
        "-c",
        "user.email=ledger@example.invalid",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "final",
    )
    _git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return path


def _origin_main(repo: Path) -> str:
    return _git(repo, "rev-parse", "origin/main")


def _final_capture(sha: str, capture_utc: str = AFTER_COMMIT) -> str:
    return (
        f"capture_utc={capture_utc}\n"
        f"final_main_sha={sha}\n"
        f"unpinned_versionz={sha}\n"
        f"lightsail_versionz={sha}\n"
        "oci_resolve_probe=down\n"
        f"ci_push_head_sha={sha}\n"
        "ci_push_last=success\n"
        f"verify_live_head_sha={sha}\n"
        "verify_live_last=success\n"
        "walkthrough_code_diff=empty\n"
        "status_links_ledger=yes\n"
    )


def _drill_evidence() -> str:
    lines = [f"head_sha={HISTORICAL_SHA}", "branch_protection=proven", "known_rows=t|t"]
    lines += [f"s{number}=PASS" for number in range(4, 22) if number not in {18, 20}]
    lines.append("s18=PASS")
    return "\n".join(lines) + "\n"


def _evidence(tmp_path: Path, final: str | None) -> Path:
    evr = tmp_path / "evr"
    evr.mkdir()
    (evr / DRILL).write_text(_drill_evidence(), encoding="utf-8")
    if final is not None:
        (evr / FINAL).write_text(final, encoding="utf-8")
    return evr


def _run(
    repo: Path, evr: Path, ledger: Path = FIXTURE_LEDGER
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(ledger), str(evr)],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
        env=GIT_ENV,
    )


def _no_go_lines(stderr: str) -> set[str]:
    return {
        line.split(":", 1)[0].removeprefix("NO-GO ")
        for line in stderr.splitlines()
        if line.startswith("NO-GO S")
    }


def test_all_go_after_the_final_capture(tmp_path: Path, repo: Path) -> None:
    evr = _evidence(tmp_path, _final_capture(_origin_main(repo)))

    result = _run(repo, evr)

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("GO 21/21\n")
    assert f"GO S4 (drill) historical: {DRILL} head_sha={HISTORICAL_SHA}" in (
        result.stdout
    )
    assert "GO S18 (drill+final-release)" in result.stdout


def test_red_before_the_final_capture_names_s1(tmp_path: Path, repo: Path) -> None:
    # Given every drill file but no final capture yet
    evr = _evidence(tmp_path, final=None)

    # When the checker runs
    result = _run(repo, evr)

    # Then exactly the final-release lines are NO-GO, S1 first, and drills stay GO
    assert result.returncode == 1
    assert result.stderr.startswith(f"NO-GO S1: evidence file absent: {FINAL}\n")
    assert _no_go_lines(result.stderr) == set(FINAL_RELEASE_LINES)
    assert "GO S4 (drill)" in result.stdout


def test_one_hex_digit_in_final_main_sha_names_s1(tmp_path: Path, repo: Path) -> None:
    # Given an otherwise all-GO capture whose final_main_sha differs by one digit
    origin_main = _origin_main(repo)
    mutated = ("0" if origin_main[0] != "0" else "1") + origin_main[1:]
    capture = _final_capture(origin_main).replace(
        f"final_main_sha={origin_main}", f"final_main_sha={mutated}"
    )
    evr = _evidence(tmp_path, capture)

    # When the checker runs
    result = _run(repo, evr)

    # Then S1 and every other final-release line go NO-GO on the SHA mismatch
    assert result.returncode == 1
    assert (
        f"NO-GO S1: final_main_sha ({mutated}) != git rev-parse origin/main "
        f"({origin_main})"
    ) in result.stderr
    assert _no_go_lines(result.stderr) == set(FINAL_RELEASE_LINES)


def test_unpinned_versionz_mismatch_names_s1(tmp_path: Path, repo: Path) -> None:
    origin_main = _origin_main(repo)
    capture = _final_capture(origin_main).replace(
        f"unpinned_versionz={origin_main}", f"unpinned_versionz={OTHER_SHA}"
    )
    evr = _evidence(tmp_path, capture)

    result = _run(repo, evr)

    assert result.returncode == 1
    assert (
        f"NO-GO S1: unpinned_versionz ({OTHER_SHA}) != final_main_sha ({origin_main})"
    ) in result.stderr


def test_capture_taken_before_the_final_commit_fails(
    tmp_path: Path, repo: Path
) -> None:
    evr = _evidence(tmp_path, _final_capture(_origin_main(repo), BEFORE_COMMIT))

    result = _run(repo, evr)

    assert result.returncode == 1
    assert (
        f"NO-GO S1: capture_utc={BEFORE_COMMIT} predates the origin/main commit"
    ) in result.stderr


def test_pending_value_in_the_capture_fails(tmp_path: Path, repo: Path) -> None:
    capture = _final_capture(_origin_main(repo)) + "hc_verify_state=PENDING\n"
    evr = _evidence(tmp_path, capture)

    result = _run(repo, evr)

    assert result.returncode == 1
    assert "NO-GO S1: hc_verify_state=PENDING is not an observation" in result.stderr


def test_missing_literal_names_only_its_line(tmp_path: Path, repo: Path) -> None:
    # Given an all-GO evidence set with one drill literal removed
    evr = _evidence(tmp_path, _final_capture(_origin_main(repo)))
    drill = evr / DRILL
    drill.write_text(
        drill.read_text(encoding="utf-8").replace("s7=PASS\n", ""), encoding="utf-8"
    )

    # When the checker runs
    result = _run(repo, evr)

    # Then only S7 is NO-GO, and it names the file and the literal
    assert result.returncode == 1
    assert f"NO-GO S7: {DRILL} lacks `s7=PASS`" in result.stderr
    assert _no_go_lines(result.stderr) == {"S7"}


def test_literal_does_not_match_a_longer_value(tmp_path: Path, repo: Path) -> None:
    evr = _evidence(tmp_path, _final_capture(_origin_main(repo)))
    drill = evr / DRILL
    drill.write_text(
        drill.read_text(encoding="utf-8").replace("s9=PASS\n", "s9=PASSED\n"),
        encoding="utf-8",
    )

    result = _run(repo, evr)

    assert result.returncode == 1
    assert _no_go_lines(result.stderr) == {"S9"}


def test_escaped_pipe_in_a_literal_is_matched(tmp_path: Path, repo: Path) -> None:
    # The fixture's S18 literal is written `known_rows=t\|t` inside the table.
    evr = _evidence(tmp_path, _final_capture(_origin_main(repo)))
    drill = evr / DRILL
    drill.write_text(
        drill.read_text(encoding="utf-8").replace("known_rows=t|t\n", ""),
        encoding="utf-8",
    )

    result = _run(repo, evr)

    assert f"NO-GO S18: {DRILL} lacks `known_rows=t|t`" in result.stderr


def test_deleted_ledger_row_is_no_go(tmp_path: Path, repo: Path) -> None:
    ledger = tmp_path / "ledger.md"
    text = FIXTURE_LEDGER.read_text(encoding="utf-8")
    row = (
        "| S9 | drill | `PILOT2-fixture-drill.txt` | `s9=PASS` | drill, historical |\n"
    )
    assert text.count(row) == 1
    ledger.write_text(text.replace(row, ""), encoding="utf-8")
    evr = _evidence(tmp_path, _final_capture(_origin_main(repo)))

    result = _run(repo, evr, ledger)

    assert result.returncode == 1
    assert "NO-GO S9: no ledger row" in result.stderr


def test_drill_line_cannot_become_final_release(tmp_path: Path, repo: Path) -> None:
    ledger = tmp_path / "ledger.md"
    text = FIXTURE_LEDGER.read_text(encoding="utf-8")
    ledger.write_text(
        text.replace("| S4 | drill |", "| S4 | final-release |"), encoding="utf-8"
    )
    evr = _evidence(tmp_path, _final_capture(_origin_main(repo)))

    result = _run(repo, evr, ledger)

    assert result.returncode == 1
    assert "NO-GO S4: drill line has a final-release row" in result.stderr


def test_real_ledger_is_well_formed_and_red_without_evidence(
    tmp_path: Path, repo: Path
) -> None:
    # Given the tracked ledger and an empty evidence directory
    evr = tmp_path / "empty"
    evr.mkdir()

    # When the checker runs
    result = _run(repo, evr, REAL_LEDGER)

    # Then all 21 lines are NO-GO, and only for absent evidence, never for structure
    assert result.returncode == 1
    assert _no_go_lines(result.stderr) == {f"S{number}" for number in range(1, 22)}
    reasons = [
        line.split(": ", 1)[1]
        for line in result.stderr.splitlines()
        if line.startswith("NO-GO S")
    ]
    assert all(reason.startswith("evidence file absent: ") for reason in reasons)
    assert result.stderr.startswith(f"NO-GO S1: evidence file absent: {FINAL}\n")

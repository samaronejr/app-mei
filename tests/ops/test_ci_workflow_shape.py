"""The default suite runs as two parallel CI jobs that together select every test.

AXRM-003 split the single `test` job (65m51s of its 75-minute ceiling) by directory:
`test` runs tests/cycles, tests/delivery and tests/integrations plus the isolation and
CNPJ steps; `test-rest` ignores exactly those directories. These tests keep the two
selections complementary, keep the guard steps identical in both jobs, keep both
required before a deploy, and keep the ceiling where it was.
"""

import shlex
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
CI_WORKFLOW: Final = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

GROUP_JOB: Final = "test"
REST_JOB: Final = "test-rest"
# Branch protection on main requires this exact check context.
GROUP_JOB_NAME: Final = "Lint, types, migrations, tests, isolation"
GROUP_PATHS: Final = ("tests/cycles", "tests/delivery", "tests/integrations")
GUARD_STEPS: Final = (
    "Checkout",
    "Assert checked-out source identity",
    "Install dependencies",
    "Bootstrap database roles",
    "Assert effective role cannot bypass RLS",
)
SUITE_STEP: Final = "Tests with coverage"
CEILING_MINUTES: Final = 75


def load_workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, f"{name!r} appears {len(matches)} times"
    found: dict[str, Any] = matches[0]
    return found


def suite_arguments(job: dict[str, Any]) -> list[str]:
    tokens = shlex.split(step(job, SUITE_STEP)["run"].replace("\\\n", " "))
    assert tokens[:3] == ["uv", "run", "pytest"], tokens
    return tokens[3:]


def selection_problems(workflow: dict[Any, Any]) -> list[str]:
    """Return why the two suite steps would not select the whole suite exactly once."""
    group = suite_arguments(workflow["jobs"][GROUP_JOB])
    rest = suite_arguments(workflow["jobs"][REST_JOB])
    # Paths are the positionals before the first option; option values follow it.
    first_option = next(i for i, a in enumerate(group) if a.startswith("-"))
    group_paths, group_flags = group[:first_option], group[first_option:]
    ignored = [a.removeprefix("--ignore=") for a in rest if a.startswith("--ignore=")]
    rest_flags = [a for a in rest if not a.startswith("--ignore=")]
    problems = []
    if tuple(group_paths) != GROUP_PATHS:
        problems.append(f"{GROUP_JOB} selects {group_paths}, not {list(GROUP_PATHS)}")
    if sorted(ignored) != sorted(group_paths):
        problems.append(f"{REST_JOB} ignores {ignored}, not {group_paths}")
    if rest and not rest[0].startswith("-"):
        problems.append(f"{REST_JOB} names paths, so it would narrow testpaths")
    if group_flags != rest_flags:
        problems.append(f"flags differ: {group_flags} vs {rest_flags}")
    if [rest[i : i + 3] for i in range(len(rest))].count(["-n", "auto", "--dist"]) != 1:
        problems.append("the suite no longer runs as -n auto --dist loadfile")
    return problems


def test_pull_requests_to_stacked_branches_run_ci() -> None:
    workflow = load_workflow()
    # PyYAML reads the bare `on:` key as the boolean True.
    branches = workflow[True]["pull_request"]["branches"]

    assert branches == ["main", "axr1/**", "axrm/**"]


def test_the_two_suite_steps_select_the_whole_suite_exactly_once() -> None:
    assert selection_problems(load_workflow()) == []
    for path in GROUP_PATHS:
        assert (PROJECT_ROOT / path / "__init__.py").is_file(), path


@pytest.mark.parametrize(
    ("job", "old", "new"),
    [
        (REST_JOB, "--ignore=tests/integrations ", ""),
        (GROUP_JOB, "tests/delivery tests/integrations", "tests/delivery"),
        (REST_JOB, "--dist loadfile", "--dist load"),
    ],
    ids=["rest-runs-a-group-directory", "group-drops-a-directory", "dist-mode-drift"],
)
def test_the_selection_guard_fires_on_drift(job: str, old: str, new: str) -> None:
    workflow = load_workflow()
    suite = step(workflow["jobs"][job], SUITE_STEP)
    assert old in suite["run"]
    suite["run"] = suite["run"].replace(old, new)

    assert selection_problems(workflow) != []


def test_both_jobs_share_services_env_and_guard_steps() -> None:
    jobs = load_workflow()["jobs"]
    group, rest = jobs[GROUP_JOB], jobs[REST_JOB]

    assert group["name"] == GROUP_JOB_NAME
    assert rest["services"] == group["services"]
    assert rest["env"] == group["env"]
    assert rest["runs-on"] == group["runs-on"]
    for name in GUARD_STEPS:
        assert step(rest, name) == step(group, name), name


def test_isolation_and_cnpj_steps_stay_in_the_group_job() -> None:
    jobs = load_workflow()["jobs"]

    isolation = step(jobs[GROUP_JOB], "Isolation suite")["run"]
    assert isolation == "uv run pytest tests/isolation/ -v -n auto --dist loadfile"
    step(jobs[GROUP_JOB], "CNPJ alfanumerico regression pack")
    assert "Isolation suite" not in [s.get("name") for s in jobs[REST_JOB]["steps"]]


def test_both_jobs_gate_the_deploy_under_the_unchanged_ceiling() -> None:
    jobs = load_workflow()["jobs"]

    assert set(jobs["deploy-pilot"]["needs"]) == {
        GROUP_JOB,
        REST_JOB,
        "container-smoke",
        "browser",
    }
    assert jobs[GROUP_JOB]["timeout-minutes"] == CEILING_MINUTES
    assert jobs[REST_JOB]["timeout-minutes"] == CEILING_MINUTES


def test_each_job_uploads_its_own_coverage_artifact() -> None:
    jobs = load_workflow()["jobs"]
    names = {
        job: step(jobs[job], "Upload coverage")["with"]["name"]
        for job in (GROUP_JOB, REST_JOB)
    }

    assert len(set(names.values())) == 2, names

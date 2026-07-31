# app-mei

Multi-tenant SaaS for Brazilian accounting firms managing MEI clients.

## Active plan

The authoritative, decision-complete work plan is
[`.omo/plans/accounting-mei-saas.md`](.omo/plans/accounting-mei-saas.md).
It supersedes [`solo-build-plan-accounting-mei-saas.md`](solo-build-plan-accounting-mei-saas.md)
(the strategy anchor) wherever the two conflict.

Background research lives in [`deep-research-report.md`](deep-research-report.md).

## Bootstrap

```sh
cp .env.example .env && docker compose up -d --wait
```

## Standing commands

| Purpose | Command |
| --- | --- |
| Lint | `uv run ruff check . && uv run ruff format --check .` |
| Types | `uv run mypy .` |
| Tests | `uv run pytest` |
| Missing migrations | `uv run python manage.py makemigrations --check --dry-run` |
| Stack up | `docker compose up -d --wait` |

## Evidence

Every todo in the plan writes QA stdout to `.evidence/<todo-id>-<happy|failure>.txt`.
The directory is git-ignored except for `.gitkeep`, so evidence is regenerated
rather than inspected from a clone.

<!-- CI gate probe: this branch exists only to produce a pull_request-event run.
     .github/workflows/ci.yml is byte-identical to main here, so the absence of
     deploy-staging in this run proves the job `if` gate, not a workflow edit.
     DO NOT MERGE. -->

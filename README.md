# app-mei

Multi-tenant SaaS for Brazilian accounting firms managing MEI clients.

## Active plan

Which plan governs, which are closed, and where the durable records live is
tracked in [`docs/project-status.md`](docs/project-status.md). Read that first:
work plans themselves are agent-local and are not part of a clone.

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

Every todo in the plan writes QA stdout to `.evidence/`, named
`<todo-id>-<kind>.txt` with four kinds:

| Kind | Contents |
| --- | --- |
| `red` | The failing run recorded before the implementation lands, for test-first todos. |
| `happy` | The passing run of the acceptance check. |
| `failure` | The forced-failure run proving the check actually bites. |
| `operator` | The record of an operator-gated step: hostnames, ids, PASS/FAIL lines and timestamps only, never secrets. |

The current plan's ids give names like `PILOT-001-happy.txt`. The directory is
git-ignored except for `.gitkeep`, so evidence is regenerated rather than
inspected from a clone.

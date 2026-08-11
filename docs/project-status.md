# Project status

What governs the work right now, which plans are finished, and where the durable
records live. This file is the tracked truth. If anything else in the repository
disagrees with it, this file wins and the other file is stale.

## Planning artifacts are local-only, on purpose

Work plans live under `.omo/plans/`, and `.omo/` is listed in `.git/info/exclude`.
That exclusion is deliberate: agent working state must not be committed, and it must
not pollute Tailwind's source detection. Consequences worth stating plainly:

- No plan file, notepad, or `boulder.json` is tracked. A clone of this repository
  contains none of them.
- An index placed inside `.omo/` could only ever be committed with `git add -f`.
  We do not do that. This document lives under `docs/` instead, which is why it is
  the index rather than a `.omo/plans/README.md`.
- The plan files themselves are immutable history once their work closes. They are
  never edited to reflect later reality. Later reality is recorded here.

Because plans are not in the clone, the sections below carry enough status for a
reader who will never see them.

## Current plan

**`pilot-readiness-v1`** — in execution. It moves the product from "deployed and
tested" to "safe to run a pilot with one accounting firm": production email,
document-vault proof and recovery, monitoring that pages a human, a rehearsed
disaster-recovery drill, a server migration ahead of an expiring cloud trial, a full
pilot walkthrough, an operations manual, and a signed release candidate. 41 numbered
todos across six waves, plus four final verification passes.

Todo identifiers from this plan use the `PILOT-###` form, and they are the ids that
appear in commit bodies and in evidence filenames.

## Plan index

| Plan | Status | Basis |
| --- | --- | --- |
| `pilot-readiness-v1` | **current** | In execution on `feat/pilot-w2-readiness` at `b4e2cbc`; 13 of 45 checkboxes fully closed, plus the repository half of four operator-gated todos (PILOT-207, 208, 301, 304) delivered while their acceptance stays open. See the note below. |
| `post-ui-ux-residual-hardening` | **closed** | All 24 todos checked. Its four pull requests merged; `main` is byte-identical to the final reviewed head. Its unfixed leftovers are written up in `docs/residual-risks.md`. |
| `ui-ux-post-completion-followups` | **closed** | All 11 todos checked. Seven findings that survived the UI/UX plan, closed as separate work. |
| `ui-ux-development-plan` | **closed** | All 39 todos checked; approved at a recorded head with the final verification wave accepted twice. The follow-ups plan above treats it as immutable and re-verifies its hash. |
| `fix-audit-failures` | **closed** | All 17 todos checked. It exists to make the failed success criteria of `accounting-mei-saas` pass with fresh evidence, and it names each one it repaired. |
| `phase-2b-todos` | **closed** | The work order decomposed from `phase-2b-portal-write`; its own status table marks stages 1 and 2 DONE and stages 3-5 are shipped in the tree (portal write access and the document vault are live). |
| `phase-2b-portal-write` | **closed** | Revision 9. All four of its open questions are marked CLOSED in the plan text; execution moved to `phase-2b-todos`. |
| `phase-2a-portal-isolation` | **closed** | Wave 1 marked CLOSED and Wave 3 marked implemented, code-reviewed, patched and deployed in the plan header. |
| `accounting-mei-saas` | **historical, partly superseded** | The original phase 0-1 build plan. Two rows of its Must-NOT-Have table are marked SUPERSEDED in place, pointing at the phase-2a and phase-2b plans. Its outstanding success criteria were closed by `fix-audit-failures`, not by itself. It is **not** the active plan, despite what older copies of the README said. |

These statuses were assigned by reading each plan's own checkboxes and status
headers. None is inferred from a filename.

### Half-done is a real state, and the current plan is full of it

Most of `pilot-readiness-v1`'s remaining todos are not blocked and not done. They split
cleanly into a repository half an agent can deliver and an operator half that needs a
real host, a real mailbox, a real bucket, or a real account — and the two halves land at
different times. Counting them as either "done" or "blocked" loses the distinction that
matters most for scheduling.

So the count above is deliberately two numbers. **13 todos are fully closed.** Four more
(PILOT-207 monitoring, PILOT-208 backup dead-man, PILOT-301 off-host copy, PILOT-304
restore-runbook gap closure) have shipped every line of code and documentation they own,
and stay open because their acceptance names evidence only the operator can produce. The
narrow remaining action is written down in each case; none of them is waiting on
engineering.

Nothing in this repository closes an operator gate. A workflow that would assert a live
deploy is not a live deploy, a script that would copy to a bucket is not a copy, and a
runbook that documents a restore is not a rehearsal.

## How changes reach `main`

**Sequential pull requests, each based on current `main`.** Never stacked.

The reason is mechanical rather than stylistic. Continuous integration is configured
to run on pull requests whose base is `main`. A pull request stacked on another
feature branch therefore receives no CI run at all, and with required checks in place
it can never merge. Rebasing a stack onto `main` to unblock it re-runs everything
anyway, so the stack buys nothing. The accepted consequence is that a wave's work
waits for the previous wave's pull request to merge before it opens.

## Suite baseline

The full test suite is the gate at every pull-request head and at the final
implementation head. Counts recorded so far:

- `main`: **1912** passing.
- `chore/pilot-w0-governance`: **1918** passing (1912 plus two tests added by
  PILOT-006 and four by PILOT-007).
- `feat/pilot-w1-email`: **1946** passing.
- `feat/pilot-w2-readiness` at `b4e2cbc`: **1994** passing. The wave-2 delta is
  PILOT-201 (+14), PILOT-202 (+12), PILOT-203 (+2), PILOT-205 (+14), PILOT-206 (+5) and
  PILOT-207 (+1), which is 1946 + 48.

Documentation-only commits must land on that last number unchanged, which is the point
of recording it per branch rather than once.

One consequence of the test setup is binding: there is a single shared PostgreSQL
test database, so test runs are strictly sequential. Two concurrent runs collide.

Documentation-only work must leave the count unchanged. Note that the guard in
`tests/scope/test_comment_references.py` scans `docs/` as well as the source roots,
so any file path or test module named in this directory has to resolve on disk.

## Durable records

| Record | What it holds |
| --- | --- |
| `docs/residual-risks.md` | Accepted risks and known gaps from the hardening work. |
| `docs/residual-risks-pilot.md` | The same, scoped to pilot readiness. |
| `docs/decisions/` | Architecture decision records. |
| `docs/lgpd.md`, `docs/retention.md` | Data-protection and retention posture. |
| `docs/regulatory-watch.md`, `docs/fiscal/` | Fiscal parameters and what to watch. |
| `docs/frontend.md` | Front-end conventions. |
| `ops/README.md`, `ops/RESTORE.md` | Operations and recovery procedures. |

<!-- External deadlines: PILOT-009 appends its dated milestone table below this line. -->

## External deadlines

Not yet recorded.

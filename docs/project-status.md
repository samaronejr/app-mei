# Project status

What governs the work right now, which plans are finished, and where the durable
records live. This file is the tracked truth. If anything else in the repository
disagrees with it, this file wins and the other file is stale.

## Planning artifacts are local-only, on purpose

Work plans live under `.omo/plans/`, and `.omo/` is listed in `.git/info/exclude`.
That exclusion is deliberate: agent working state must not be committed, and it must
not pollute Tailwind's source detection. Consequences worth stating plainly:

- No plan file, notepad, or `boulder.json` is tracked. A clone of this repository
  contains none of them — except the six oldest `.omo` files (the phase-2a/2b
  plans and drafts, plus `accounting-mei-saas`), which were committed before the
  exclusion existed and stay tracked as frozen history. Every plan since,
  including both pilot-readiness plans, is local-only.
- An index placed inside `.omo/` could only ever be committed with `git add -f`.
  We do not do that. This document lives under `docs/` instead, which is why it is
  the index rather than a `.omo/plans/README.md`.
- The plan files themselves are immutable history once their work closes. They are
  never edited to reflect later reality. Later reality is recorded here.

Because plans are not in the clone, the sections below carry enough status for a
reader who will never see them.

## Current plan

Two plans are current, and they run on separate tracks: `pilot-readiness-v2`,
described next, and `axorim-pivot-r1`, described under "Axorim pivot" below.

**`pilot-readiness-v2`** — in execution. It supersedes `pilot-readiness-v1` and the
Resend amendment, and it starts from what is actually true rather than what v1
assumed: the latest code has never been deployed anywhere, the old OCI host still
serves a month-old release, no email has ever been sent, and the live-verification
workflow has been red for weeks. It ends with one accounting firm able to start a
controlled pilot on the new Lightsail server, every safety claim proven by a real
effect rather than asserted. 41 numbered rows across six waves, plus four final
verification passes.

Row identifiers from this plan use the `PILOT2-###` form in commit bodies and
evidence filenames. Rows carry a marker: `[E]` is an engineering row, `[O]` is an
operator gate that no repository change can close, and `[A]` is an agent-executed
external effect. Two owner decisions shape the whole plan: email goes through
Resend's plain SMTP relay (no new email code), and the deploy target is the
already-provisioned Lightsail instance `app-mei-pilot-sa-east-1` at 18.229.187.66,
with the OCI host kept intact after cutover as the rollback anchor.

All 41 rows are closed, including the operator-run Lightsail Phase B (row 16) and
the deploy-pipeline cutover (row 17, merged as PR #21, which triggered the first
deploy to Lightsail). The go/no-go ledger reports every S-line GO on the final
capture. What remains is the F1-F4 final-verification wave and, after it, the
owner's authorization of the pilot start; the `v0.2.0-rc1` tag and release follow
that authorization per `ops/RELEASES.md` Phase B.

## Axorim pivot

The product direction changed on 2026-09-27. Product work is now governed by the
local-only plan **`axorim-pivot-r1`**, which, like every plan since the exclusion,
lives under `.omo/plans/` and is never committed. "Axorim" is an internal working
name. The repository, the README headline and customer-facing copy stay "app-mei"
until the name is cleared.

- **Ids.** The plan has 29 numbered todos in five waves, plus a final verification
  wave. Todo N is cited as `AXR1-NNN` (todo 1 is `AXR1-001`) in commit bodies and in
  evidence filenames of the form `.evidence/AXR1-NNN-<kind>.txt`.
- **Delivery.** One pull request per todo, based on current `main`, with every new
  surface behind a per-tenant feature flag that's off by default. Each pull request
  updates the progress line below and the suite baseline.
- **Records.** `docs/product/pivot-supersession.md` says which earlier restrictions are
  superseded and which still bind. ADRs 0002 to 0007 in `docs/decisions/` hold the
  architecture decisions, and `docs/product/vocabulary.md` holds the status words.
- **Pilot obligations stay open and separate.** The `pilot-readiness-v2` F1-F4 wave
  and the owner's authorization of the pilot start are neither closed, waived nor
  absorbed by the pivot. They're decided on their own track. R1 reaches the pilot
  tenant only behind its flag, and only after that authorization.

Progress: `AXR1-001` (the pivot records) lands with the commit that adds this
section. `AXR1-002` (per-tenant feature flags with a global kill switch;
`tests/core/test_flags.py`; `.evidence/AXR1-002-red.txt`, `-happy.txt`,
`-failure.txt`, `-gates.txt`, and `-adversarial.txt`), `AXR1-003` (the R1 design
tokens, `templates/partials/ui/` components and the flagged product shell, plus the
browser and accessibility harness; `tests/ui/test_product_shell.py`,
`tests/ui/test_ui_partials.py`, `tests/test_support_threads.py`,
`tests/browser/test_shell.py` and `tests/browser/test_components.py`;
`.evidence/AXR1-003-baseline.txt`, `-happy.txt`, `-desktop.png`, `-mobile.png`,
`-failure.txt`, `-failure-role.txt`, `-flagoff.txt`, `-mutation.txt`,
`-ci-browser-job.txt`, `-gates.txt`, and `-adversarial.txt`), `AXR1-004` (the 13
S8.1 capabilities and per-user capability grants; `tests/authz/test_r1_capabilities_positive.py`,
`tests/authz/test_user_capability_grant.py`; `.evidence/AXR1-004-red.txt`,
`-happy.txt`, `-failure.txt`, `-gates.txt`, and `-adversarial.txt`) and `AXR1-030`
(certificate metadata-only guard; `tests/clients/test_certificate_metadata_only.py`;
`.evidence/AXR1-030-happy.txt`, `-failure.txt`, `-gates.txt`, and `-adversarial.txt`) are
recorded. `AXR1-005` (the client spine: `Engagement`, the effective-dated
`TaxProfile` and `ClientContact` with the provenance-marked backfill;
`tests/clients/test_tax_profile_transition.py` and
`tests/clients/test_backfill_v1.py`; `.evidence/AXR1-005-red.txt`, `-happy.txt`,
`-failure.txt`, `-failure-ext.txt`, `-failure-template1.txt`, `-plan.txt`,
`-adversarial.txt`, `-gates.txt`, and `-baseline.txt`) is recorded, with OG-10 —
the pilot database's one-time `CREATE EXTENSION` step — still open. `AXR1-006`
(the profile-driven calendar and threshold readers, revision-checked registry
dual-write, and client-isolated payment observations) is recorded on the T5
stack. Pilot payment rendering uses observation provenance and verification,
never work status; manual claims stay neutral. The gate repairs cover stale
admin POSTs, portfolio authorization before matching-profile returns, shared
audit-function preservation on rollback, and a genuinely failing drift guard.
Evidence: `.evidence/AXR1-006-fix-1.txt`, `-fix-2.txt`, `-fix-3.txt`, `-fix-4.txt`,
`-fix-5.txt`, `-failure.txt`, `-placement-rebase.txt`, `-placement-collection.txt`,
`-placement-related.txt`, `-placement-migration.txt`, and `-placement-gates.txt`.
The populated rollback/reapply retained obligations, attached documents and both
audit streams. Todos 7 to 29 and the final verification wave remain open.

`AXR1-004` gate repairs (`2f4cf92`) add a locked membership grant revision and
409 conflict feedback, restore the original owner query budgets, and mirror the
portal NONE decision. Reversing the R1 seed removes only R1 grants (active and
revoked), retaining audit history and non-R1 data; reapplying does not restore
removed authority. Evidence: `.evidence/AXR1-004-fix-1.txt` through `-fix-5.txt`,
`-fix-repeat.txt`, `-fix-mutations.txt`, `-fix-browser.txt`, and `-fix-gates.txt`.

## Plan index

| Plan | Status | Basis |
| --- | --- | --- |
| `axorim-pivot-r1` | **current** (product direction) | Started 2026-09-27. 29 todos in five waves plus a final verification wave; `AXR1-001` recorded the pivot. It runs separately from the open F1-F4 wave and pilot authorization of `pilot-readiness-v2`. |
| `pilot-readiness-v2` | **current** | Execution complete: all 41 rows closed and `ops/check_go_no_go.py` exits 0 against the evidence directory, every S-line GO on the final capture. Only the F1-F4 audits and the owner's authorization remain. |
| `pilot-readiness-v1` | **superseded** | Replaced by `pilot-readiness-v2` on 2026-09-21. Its checkbox states are frozen and its evidence is kept, never rewritten; v2's legacy reconciliation ledger classifies every v1 row (13 verified-complete, 5 repo-complete with operator evidence open, 12 partial, 8 open-valid, 2 superseded, 2 invalid-premise, plus the four F-rows that never ran). |
| `resend-smtp-pilot-provider-amendment` | **superseded** | Superseded by owner decision D-Q1: Resend is used through the existing provider-neutral SMTP settings, so the amendment's new email machinery is unneeded. Its branch `chore/pilot-resend-smtp-round3h` is frozen, unpushed, and never to be merged. |
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

### `open (needs …)` is a real state, and the plan ran on it

While `pilot-readiness-v2` was in flight, most of its rows were neither blocked nor
done. The plan
splits them by who can close them: `[E]` rows an agent can deliver, `[O]` rows that
need a real host, a real mailbox, a real bucket, or a real account — and the two
land at different times. An operator row reads `open (needs <account|host|mailbox|
bucket|window>)` until its evidence artifact exists; it is never `blocked`, and an
engineering row that only prepares a gate is never blocked by the gate being open.

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

## Deploy cutover state

The push deploy in `ci.yml` (`deploy-pilot`) now targets the Lightsail pilot host
through the OIDC just-in-time firewall; `deploy-lightsail.yml` is slimmed to the
OIDC claim probe and the stale-rule cleanup, and the OCI-era `DEPLOY_*` secrets are
retired. Both states that were expected after the cutover merge have played out:

- **The shadow deploy happened.** The first push to `main` after the merge deployed
  `c7afe36` to Lightsail. The first attempt failed assert 4 because no certificate had
  been issued yet (the Cloudflare token was IP-restricted); after the token fix, the
  re-run passed all six asserts and the storage probe (`.evidence/PILOT2-403-happy.txt`).
- **`verify-live` is green.** It was red while the public edge still served the old
  OCI release. DNS flipped to Lightsail on 2026-09-22, and the scheduled run of
  2026-09-23 passed against the target (`.evidence/PILOT2-406-operator.txt`).

## Cutover record

last_premerge_sha=c63326d4a2372609a12b7d34cfacd9f46fc7adc1
window_utc=2026-09-23T14:00:00Z
rollback_deadline=2026-09-23T18:00:00Z
rollback_trigger=any step-7 check red OR any step-10 check red
rollback_semantics=rollback to the old host restores data as of freeze_utc; any write accepted on the target after the flip is lost; therefore NO pilot-firm access before row 34 closes
freeze_sha=aff146440f9f99f76df5a790146cbad8aada6174
freeze_sha_source=.evidence/PILOT2-404E-happy.txt
executed_window_utc=2026-09-22T13:15:00Z
executed_rollback_deadline_utc=2026-09-22T17:15:00Z
freeze_started_utc=2026-09-22T14:18:55Z
dns_flip_completed_no_later_than_utc=2026-09-22T14:36:59Z
rollback_invoked=no
cutover_status=closed
old_host_state=stopped-intact
old_host_termination_not_before=v0.2.0-rc1 tag date + 7 days (tag not yet created)

`freeze_sha` is the merged row-27 commit, backfilled from `.evidence/PILOT2-404E-happy.txt`
and repeated in `.evidence/PILOT2-404-operator.txt`. The `window_utc` and
`rollback_deadline` lines are the plan as first recorded. The owner then moved the
cutover forward a day, and the `executed_*` lines are what ran
(`.evidence/PILOT2-404-operator.txt`, `.evidence/PILOT2-404O2-operator.txt`). The OCI
host was stopped with its disk intact on 2026-09-23 (`.evidence/PILOT2-401-operator.txt`);
its termination date gets fixed once the `v0.2.0-rc1` tag exists.

The authoritative 12-step cutover table, with its as-executed record, is the ordered
state machine under "Cutover: data freeze, restore, and DNS flip" in `ops/README.md`.

## Suite baseline

The full test suite is the gate at every pull-request head and at the final
implementation head. The current default-suite baseline is **2329 passing**,
verified by one serial full run on the placed `AXR1-006` code head `8b968d0`.
Collection on T5 (`1222815`) found **2266** default tests; T6 adds **63** net-new
cases (33 from the reader/observation change and 30 gate-review regressions).
Both collections deselected the same 26 browser cases. The separate browser
run passed **26**, isolation passed **133**, and the CNPJ pack passed **29**.
Evidence: `.evidence/AXR1-006-placement-collection.txt` and
`.evidence/AXR1-006-placement-gates.txt`. The preceding T5 count was the **2228**
post-repair T4 baseline plus thirty-eight net-new tests from `AXR1-005`
(the client spine and its gate-review fixes).

The pre-repair baseline is **2213** passing, reproduced at `9995488` on 2026-09-27
(`.evidence/AXR1-004-fix-baseline.txt`). Originally recorded at `c428733` by
`AXR1-004`, it comprises the T3 baseline of 2100 plus one hundred
thirteen new tests: seventy-nine parametrized matrix cells, eleven positive
resolution tests, fifteen grant tests, six manager-wiring cases and three
RLS-coverage cases for `authz_usercapabilitygrant`, minus one replaced
cell-count sentinel. `AXR1-004` gate repairs (`2f4cf92`) carried it to **2228**
with fifteen gate-review regressions; committed-head gate evidence for that
repair lives in `.evidence/AXR1-004-fix-gates.txt`.

Browser tests are a separate suite. `uv run pytest` deselects them (`addopts` carries
`-m "not browser"`), so they are not part of the count above. They run with
`uv run pytest -m browser` after `uv run playwright install chromium`, and in CI in the
`browser` job. The suite now contains **26** cases: the **24** passing at the T3
commit `bff2bb2`, plus the two-viewport reviewer-conflict regression. Every test in
`tests/browser/` runs at 1440x900 and at 390x844.

The rule is about the delta, not a fixed number. A documentation-only change must
leave the latest recorded count unchanged. A change that adds tests raises it by
exactly the tests it adds, and that pull request records the new count here with its
delta. A count that drops without a recorded, classified reason is a failure.

History. The `pilot-readiness-v2` baseline was **2024** passing, a clean serial run on
merged `main` at `b19edf3` and later. Later pull requests added tests without updating
that figure, which is how it drifted to 2053 and why the rule now follows the latest
recorded count.

Counts recorded under `pilot-readiness-v1`, kept for provenance:

- `main`: **1912** passing.
- `chore/pilot-w0-governance`: **1918** passing (1912 plus two tests added by
  PILOT-006 and four by PILOT-007).
- `feat/pilot-w1-email`: **1946** passing.
- `feat/pilot-w2-readiness` at `b4e2cbc`: **1994** passing. The wave-2 delta is
  PILOT-201 (+14), PILOT-202 (+12), PILOT-203 (+2), PILOT-205 (+14), PILOT-206 (+5) and
  PILOT-207 (+1), which is 1946 + 48.

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
| `docs/product/pivot-supersession.md` | The Axorim pivot: superseded restrictions, constraints still in force, and R1's engineering scope. |
| `docs/product/vocabulary.md` | The pt-BR status vocabulary and the rules for success words. |
| `docs/lgpd.md`, `docs/retention.md` | Data-protection and retention posture. |
| `docs/regulatory-watch.md`, `docs/fiscal/` | Fiscal parameters and what to watch. |
| `docs/frontend.md` | Front-end conventions. |
| `ops/README.md`, `ops/RESTORE.md` | Operations and recovery procedures. |
| `ops/PILOT-GO-NO-GO.md` | The pilot go/no-go ledger: one row per `pilot-readiness-v2` Success-criteria line (S1-S21). `ops/check_go_no_go.py` checks it against the untracked evidence and the final capture; exit 0 is the precondition for the F-wave. |

## External deadlines

`pilot-readiness-v1` kept a checklist of seven dated checkpoints here, every one of
them a formula over two operator-supplied dates. `pilot-readiness-v2` row 2 resolved
them against the real consoles on 2026-09-21, and the resolved ledger below replaces
the formulas. No external clock still binds the schedule; the OCI trial mooted
itself.

| # | Checkpoint | Resolved state |
| --- | --- | --- |
| 1a | OCI trial expiry read from the billing console. | **Resolved.** The trial ended 2026-08-28 and the host survived on the always-free tier. The instance was stopped on 2026-09-23 with its volumes intact; its termination date is set by the `v0.2.0-rc1` tag decommission window, and no reclamation notice is pending. |
| 1b | OCI storage disposition decided. | **Resolved: keep-oci.** The OCI documents bucket stays the document store — versioning enabled, a 30-day previous-versions lifecycle rule, and the free storage entitlement persists. There is no migration to AWS; v2's conditional move row (15) is skipped by this ruling. |
| 2a, 2b | SES production access submitted, then reviewed. | **CLOSED-ABANDONED.** The appeal is closed by owner decision D-Q1: mail goes through Resend SMTP instead, and no SES checkpoint remains. |
| 3 | UptimeRobot capability confirmed. | **Resolved, as keyword substring.** UptimeRobot cannot evaluate JSONPath; its only body-inspection primitive is a substring match, so the gate is keyword capability, not JSON assertion. Four keyword monitors plus the impossible-keyword negative control are configured per `ops/README.md` "External uptime monitoring". |
| 4 | Healthchecks.io account and first check. | **Resolved.** The account exists and the backup dead-man checks are live with transitions proven; v2 adds target-scoped checks so the two hosts never share one. |
| 5a | VPS ordered. | **Resolved.** The Lightsail instance `app-mei-pilot-sa-east-1` (18.229.187.66) is provisioned and running, static IP attached, firewall open on 80/443 and operator SSH. |
| 5b | Base-host bootstrap, Phase A. | **Resolved.** Phase A ran on the Lightsail host (memory preflight passed, swap remediated) and the stack was then torn down ahead of Phase B, which is v2 row 16 and has since closed. |
| 6a, 6b | Binding go/no-go at `E−5`; cutover by `E−3`. | **Superseded.** `E` has passed with the host alive, so both formulas are moot. The cutover window, freeze, and rollback deadline are set by v2 row 27 instead. |
| 7 | Same-day escalation of any missed checkpoint. | **Standing.** Unchanged: a missed checkpoint reported late is two failures, not one. |

The Let's Encrypt certificate on the OCI host expires 2026-10-27. The cutover ran
on 2026-09-22, well inside that window, and the public name is now served by
Lightsail, where Caddy issues its own certificate. The date stays only as
provenance for this ledger.

### The UptimeRobot negative control

The capability proof is a monitor that must stay DOWN. It points at the same
`https://samaronefialho.dev/healthz` URL on the same interval with the same
alert-on-absence setting as the four real monitors, keyed on a keyword the endpoint
can never render — `scheduler` only ever renders `alive`, `stale` or `unknown`, so
the impossible-keyword string documented in `ops/README.md` cannot appear in any
body. Acceptance is four UP and one DOWN, and the DOWN one is required: if the
control ever reports UP, keyword evaluation is not happening and the four green
monitors are asserting nothing.

### Where each procedure lives

| Checkpoint | Procedure |
| --- | --- |
| 2a, 2b (mail) | `ops/README.md`, "Email provider (Resend SMTP)" — the SES paragraphs are kept at the end of that section as history. |
| 1b (OCI storage) | `ops/README.md`, "Object storage: versioning, lifecycle, probe". |
| 5a (VPS order) | `ops/README.md`, "Host requirements". |
| 5b (bootstrap) | `ops/README.md`, "New host bootstrap (two phases)". |
| 6b (cutover aftermath) | `ops/README.md`, "Decommissioning the old host", and "Host log rotation" for the new host's retention. |
| 3, 4 (monitoring) | `ops/PILOT-RUNBOOK.md`, [monitor-to-action mapping](../ops/PILOT-RUNBOOK.md#monitor-to-action). |
| 1b, 6b (pilot exit) | `ops/PILOT-RUNBOOK.md`, [pilot exit and conversion](../ops/PILOT-RUNBOOK.md#pilot-exit-and-conversion). |

Operator outcomes are recorded to the execution worktree's `.evidence/` root as
`PILOT2-###-operator.txt` files — ids, hostnames, and PASS/FAIL lines only, never
secrets.

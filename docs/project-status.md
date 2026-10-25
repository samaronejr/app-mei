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

**`axorim-regional-mvp` is the current plan**, approved by the owner on 2026-10-22
and described under "Regional MVP" below. `axorim-pivot-r1` was delivered and merged
on 2026-10-15 at `00f1d41`; the backup fix merged on 2026-10-21 at `71225ef`.
These release facts are recorded in `.evidence/AXR1-merge-release-final-20261015.json`
and `.evidence/AXR1-backup-fix-final-20261021.json`, not new cohort authorizations.

**`pilot-readiness-v2` is historical release-specific readiness.** Its 41 execution
rows were recorded closed, and F1-F4 are checked complete in the immutable v2 plan
`/home/samarone/Documents/app_mei/.omo/plans/pilot-readiness-v2.md:534-543`.
`/home/samarone/Documents/app_mei-pilot2/.evidence/PILOT2-506-final.txt:1,22-24`
records `pilot_start_authorized=2026-09-26` for
`a962173878f9c0cfcfcd11406aba3c7a95701454`, also the target of tag `v0.2.0-rc1`.
That authorization predates R1 and does not cover `71225ef` or the regional MVP.

AXRM-002 captured public `/versionz` on 2026-10-22 at 10:53:17 UTC:
`71225ef4272cf678bc23b7f927ab5b1e0767a323`, equal to `origin/main` at capture
time (the local execution branch was at `014123f`). At 10:53:38 UTC the latest
`verify-live` run listed was **39611651487**, scheduled 2026-10-21 at 13:51:20 UTC,
completed successfully at 13:51:29 UTC on `71225ef`. This is a dated observation,
not a fresh health drill or permission to activate a cohort.

Running `ops/check_go_no_go.py` against the unchanged v2 evidence on 2026-10-22
exits **1**: **S1, S2, S3, S18 and S20 are NO-GO**, because the final capture names
`a962173` rather than current `origin/main` and predates that commit. Historical
drill evidence is not a current-release pass. OG-02 remains
**open (closes by the owner's OD-02 decision: the v2 obligations carry to the current
release, or a re-run)**. Source-by-source reconciliation and the complete NO-GO
output are in `.evidence/AXRM-002-pilot2-reconciliation.txt` and
`.evidence/AXRM-002-failure.txt`. No v2 row was re-run and no production state changed.

## Regional MVP

On 2026-10-22 the owner approved the local-only plan **`axorim-regional-mvp`**, which
builds on the delivered R1 release (origin/main `71225ef`). It adds federal checks
through SERPRO Integra Contador, fiscal document capture through Focus NFe and a
municipal workbench, for one accounting firm with clients in a bounded set of Rio de
Janeiro municipalities. Like every plan since the exclusion, it lives under
`.omo/plans/` and is never committed.

- **Ids.** Todo N is cited as `AXRM-NNN` (todo 1 is `AXRM-001`) in commit bodies and in
  evidence filenames of the form `.evidence/AXRM-NNN-<kind>.txt`. Gate evidence uses
  `.evidence/AXRM-<gate id>-<kind>.txt`.
- **Delivery (owner answer Q3, 2026-10-22, "Merge per wave").** One PR per todo,
  stacked linearly within a wave; the first PR of a wave is based on the current
  `main`. When every PR of a wave is green and reviewed, the owner authorizes merging
  that wave. Each merge deploys to the pilot host with every new flag off and
  `PROVIDERS_PAUSED=True`. No force pushes, and no merge without that authorization.
- **Records.** ADR 0008 (provider boundary, operation allowlist, credential custody and
  threat model), ADR 0009 (municipal rules as reviewed firm data), the amendments to
  ADRs 0003 and 0006, the regional section of `docs/product/pivot-supersession.md`, the
  regional labels in `docs/product/vocabulary.md`, and the "Regional MVP" section of
  `ops/AXORIM-R1-GO-NO-GO.md`, which holds every regional gate.
- **Gates stay open.** Engineering work prepares the provider-qualification, operator,
  live-qualification and owner gates; it closes none of them.

Progress: `AXRM-001` (the regional records) lands with the commit that adds this
section. `AXRM-002` reconciles the historical v2 authorization with the deployed
release and prepares OD-02; OG-02 remains open.

`AXRM-003` (CI headroom, promotes R1 T83/T90): R1's single test job used 65m51s of
its 75-minute ceiling (run 38638197711: suite 53m52s, isolation 10m01s). The default
suite now runs as two parallel required jobs under the same ceiling: `test` (name
unchanged, the check branch protection requires) selects `tests/cycles`,
`tests/delivery` and `tests/integrations` and keeps lint, types, checks, migrations,
the isolation step and the CNPJ pack; `test-rest` ignores exactly those directories.
Both carry the same services, env, source-identity, role-bootstrap and RLS-bypass
steps, and `deploy-pilot` needs both. Pull requests into `axrm/**` now run CI.
Collected at this commit: 5292 default tests (1186 + 4106, node-id sets disjoint and
equal to the single-job selection plus this todo's 9 shape tests) and 177 browser
tests. `tests/ops/test_ci_workflow_shape.py` pins the complement, the shared guards,
the deploy needs and the ceiling. Browser screenshots now wait for `networkidle` (skipped only for
the three captures of a request deliberately held in flight, which pass
`mid_request=True`) and a laid-out `body`, and retry only Chromium's `Page.captureScreenshot` "Unable to capture
screenshot" protocol error, once, keeping the first failure as a
`*.capture-error.txt` artifact beside the screenshots. Recovery: revert to the single
job. Adding the `test-rest` check to main's required status checks is an owner
setting, still open.

`AXRM-004` (EG-5 baseline, promotes R1 T91): the existing harness (`ops/perf`, the
mobile driver in `tests/perf`) ran under the gate lock at `88f9f03` on a disposable
constrained lane (PostgreSQL 320 MiB, Redis 96 MiB, web one CPU/400 MiB, image built from
`git archive` of that SHA) with `seed_axorim_demo --firms=growth --seed=42`: 200 samples per
read, ten synthetic actors per role. The p50/p95 values below are server time in ms; the
last two columns are the per-sample maxima.

| Read | p50 | p95 | Max queries | Max gzip bytes |
| --- | ---: | ---: | ---: | ---: |
| Hoje | 473.2 | **517.4** | 23 | 12159 |
| Clientes | 15.9 | 17.7 | 16 | 4653 |
| Clientes search | 12.7 | 14.5 | 16 | 2675 |
| Client workspace | 40.6 | 87.2 | 19 | 4995 |
| Cycle detail | 24.7 | 67.1 | 23 | 6741 |
| Portal Inicio | 14.6 | 15.8 | 23 | 3214 |
| Hoje period filter, click to populated swap | 806.5 | 881.6 | 23 | 11728 |

Hoje's p95 misses the <500 ms server target at this head and stays an open follow-up;
no target was raised and no cause is claimed. Another project's tests shared the host
(load average 12-16 on 32 cores, recorded before and after). Every other read is within
its targets. These are constrained-local numbers, not a production SLO.
`tests/perf/test_regional_read_budgets.py` holds workspace and cycle detail to 25 queries
and 150,000 gzip bytes, measured on the cold first request against
`tests/perf/regional_world.py`: the seeded small firm's portal client with 31 requests in
its competence (currently 21 and 25 queries, so cycle detail has no headroom). Later
regional todos add their rows and witnesses to that world. A per-row cycle fetch injected
into the workspace request section fails the budget at 52 queries. Separately, the T36
fully populated cycle-detail fixture costs 32 queries on both reads, constant in row count;
that fixture is not under a 25-query assertion, and this item records the count without
changing it. Suite at this item: 5253 passed, 41 skipped (5251 before, plus the 2 new
cases); isolation 722 passed, 41 skipped; CNPJ 29. Recovery: revert (tests and local
measurement only).

`AXRM-005` (required integrated scanning, promotes R1 T70/T85 and the scanner part
of T80): a new per-firm flag `scan_required` (off by default; binds only with
`cycles_r1`). For a flagged firm, every portal upload (request and vault) is refused
with "Envio temporariamente indisponível: a verificação antivírus está
desligada." while `CLAMAV_ENABLED` is off, before any byte or row is stored; staff
never download a `verificacao_indisponivel` or never-scanned file (403 refusal on GET
and POST, no warning dialog); exports keep excluding unscanned originals. Cycle
detail and document review name each file's intake state without a risk download
(T70); the vault refusal page distinguishes missing, oversized, refused type and
scanner off by a machine reason (T85). The beat task `ping_scanner` (PING + VERSION)
feeds a "Verificador antivírus" row on the Automações page with last success and
signature version, never `/readyz`; the compose `clamav` service gains the pinned
image's own `clamdcheck.sh` healthcheck (memory limit unchanged); and `manage.py
scanner_selfcheck --tenant <synthetic>` sends EICAR and a clean PDF through the real
intake for operator gate OG-R1, which stays open (`ops/README.md`, "Required scanning
per firm"). Unflagged firms keep the R1 behaviour byte for byte, including the
warning download. Recovery: flag off. Production settings and the `CLAMD_*` defaults
are unchanged. Suites at this commit: default 5332 collected, 5291 passed and 41
skipped (`test` 1216, `test-rest` 4075 with 41 skipped; 40 new cases), isolation 722
passed and 41 skipped, CNPJ 29, browser 181 (2 new cases at both widths), static build
clean. The first full run failed three static source guards on the new operator
command only; the one-file fix was re-gated under the lock (1414 passed). Evidence:
`.evidence/AXRM-005-gates.txt`, `AXRM-005-gates-fix.txt`.

Review fix (`AXRM-005` attempt 2, independent review needs-fix): under `scan_required`
the portal serves only scanner-cleared bytes (the R1 firm-upload exemption for
originals without an intake now applies only with the flag off), and `record_review`
refuses a `verificacao_indisponivel` or never-scanned file with the typed
`UnscannedReviewRefused` (409, no row written); cycle detail, the review queue and the
review page drop the review affordance for those files. Flag-off behaviour is
unchanged. Suites after the fix: default 5343 collected, 5302 passed and 41 skipped
(`test` 1227, `test-rest` 4075 with 41 skipped; 11 new cases), isolation 722 passed and
41 skipped, CNPJ 29, browser 181 (5343 deselected), static build clean. Evidence:
`.evidence/AXRM-005-red-fix.txt`, `AXRM-005-mutations-fix.txt`, `AXRM-005-flaky-fix.txt`,
`AXRM-005-gates-r2.txt`.

| Todo | Engineering delivery | Rollout and recovery |
| --- | --- | --- |
| `AXRM-006` | Rio metro registry identities, client create/edit and import validation, and the municipality name in the workspace header. Verification results are recorded below. | Apply `fiscal.0003_seed_rio_metro`; no capability or municipal coverage is asserted. Reverse removes only the registry rows the forward actually inserted (recorded in `fiscal_municipalityseedrow`), so Rio de Janeiro from the capital seed, unrelated municipalities and a metro row registered beforehand survive, and it refuses while a client or related registry record references an inserted row. Referenced data requires forward repair. |

The pre-change baseline for `AXRM-006` is the documented **5148 passed, 41 skipped,
168 deselected**, accepted on 2026-10-22 without a local re-run because base
`014123f` changes only documentation relative to the delivered code. At the
`AXRM-006` code the default suite passes **5267** (41 skipped, 174 deselected),
isolation **722 passed, 41 skipped**, CNPJ **29 passed** and browser **174 passed**;
lint, format, Mypy, migration drift and the static rebuild are clean. The
municipality pack (registry plus form/import validation) passes **50 tests in each of
three consecutive runs**. A populated forward/reverse/forward round trip removes only
the fifteen new codes, keeps the capital and unrelated rows, and refuses the reverse
while a client or a capability row references a seeded code. The CSV import accepts a
Niteroi row and shows "Niterói/RJ" in the workspace header, and refuses an unknown
code at row level with nothing created, at 1440x900 and 390x844. Evidence:
`.evidence/AXRM-006-gates.txt`, `AXRM-006-targeted.txt`, `AXRM-006-migration.txt`,
`AXRM-006-happy.txt`, `AXRM-006-failure.txt`, `AXRM-006-adversarial.txt`.

Review fix (`AXRM-006` attempt 2, independent review reject): the reverse of
`fiscal.0003_seed_rio_metro` deleted a metro row that existed before the migration.
The forward now records each row its upsert created in a platform-level ledger,
`fiscal_municipalityseedrow` (allow-listed, no tenant data, no foreign key), which 0003
creates and drops itself, and the reverse deletes only those rows while keeping the
client and related-record refusals. A saved CSV preview committed after its codes left
the registry now records "Falha ao gravar: Município não encontrado no cadastro
(código IBGE)" instead of the exception class name; other commit failures are
unchanged. Suites after the fix: default **5330 passed, 41 skipped** (the first run
failed only the scope test's pin of fiscal models, which now registers the ledger),
isolation **722 passed, 41 skipped**, CNPJ **29**, `tests/clients` plus the registry
pack **216**, and the municipality pack **51** in each of three runs. Workspace reads
take 22 of 25 queries and cycle detail 25 of 25. Lint, format, Mypy and migration
drift are clean. An `app_migrator` round trip on a populated two-tenant database keeps
a pre-existing Niterói through the reverse and a second forward/reverse cycle, and a
fresh database records exactly the fifteen codes 0003 inserts. Evidence:
`.evidence/AXRM-006-red-fix.txt`, `AXRM-006-mutations-fix.txt`,
`AXRM-006-flaky-fix.txt`, `AXRM-006-gates-fix.txt`, `AXRM-006-fix-*.log`.

Review fix (`AXRM-006` attempt 3, re-review blocker B3): the ledger table had no
explicit portal decision. The portal already had no grant on it; the decision is now
declared as `-- PORTAL_NONE: fiscal_municipalityseedrow` in `ops/sql/roles.sql` and in
`PORTAL_DENIED_TABLES`. A new meta-test requires every model table to carry a portal
decision (a frozen pin lists the tables that predate the rule), so a table missing
from both registries now fails; removing the conftest entry fails it and the parity
test. Suites: isolation **724 passed, 41 skipped** (722 plus the two new cases),
`tests/portal` **319**, the portal meta-tests **7**, and the municipality pack **31**.
Lint, format, Mypy and migration drift are clean. Evidence:
`.evidence/AXRM-006-red-fix3.txt`, `AXRM-006-mutation-fix3.txt`,
`AXRM-006-fix3-*.log`, `AXRM-006-fix3-static.txt`.

### Wave 1: provider foundation

| Todo | Engineering delivery | Rollout and recovery |
| --- | --- | --- |
| `AXRM-007` | New `apps/integrations` with `ProviderConnection` (tenant) and `ClientProviderLink` (client): tenant RLS, portal NONE (`DenyPortalAccess`, `-- PORTAL_NONE:` in `ops/sql/roles.sql`, `PORTAL_DENIED_TABLES`), composite (tenant, parent) foreign keys, version-checked locked transitions for the link's three A7.3 state fields, a capture start month that a database trigger keeps immutable, and a `secret_ref` that holds a secret *name* only (validator plus CHECK). Flags `federal_checks`, `document_capture` and `municipal_work`, each inert without `cycles_r1` and `scan_required` (INV-R13). Closed provider settings (`PROVIDERS_PAUSED=True`, environments `fake`, no real-data approval, no cap) and the `integrations.E001`/`E002` startup checks. Five A7.6 capabilities (matrix 36 rows / 216 cells, seeded by `authz.0009`). A read-only "Conexões fiscais" panel on `/configuracoes/` for holders of `fiscal.connections.manage` while a regional flag is on. | Tables ship empty, flags off, providers paused. `integrations.0001` reverses while both tables are empty and refuses (with a test) once a row exists: recovery is then flags off with the tables kept (forward repair). `authz.0009` records what it created or changed in the platform ledger `authz_capabilityseedrow`; its reverse deletes only the capabilities and role cells it created, puts back any label or level it changed, and refuses (forward repair) while a firm holds a user grant on a capability it created. |
| `AXRM-012` | Document and obligation portal visibility is enforced in both restrictive policy clauses, and quarantine metadata, requests, links and observations follow the obligation or document they reference; provider originals use actor-less quarantined intake with receipt provenance. | Apply `obligations.0023_document_portal_visibility` before captured or municipal rows exist. Existing rows keep their visibility. Reverse restores the previous policies and trigger body, and refuses while provider-origin documents, private documents (including visible/private hash pairs) or private obligations exist; retained new data requires forward repair. |
| `AXRM-008` | Client authority as append-only revisions (`AuthorityRecord`: procurações 00002/00004, PGMEI catalogued `nao_exigida`, client authorizations for NF-e/NFS-e capture and Ciência), the firm termo record (`FirmAuthorization`, submitted by todo 13) and owned provider problems (`IntegrationIssue` when the client has no open cycle, the registered cycle exception when it does). `authority_for(client, operation)` answers usable / missing / expired / unknown with a reason, fails closed for any operation outside the implemented A6 set, derives expiry from the injected date and, for capture, also requires a current, matching, non-divergent provider-held certificate. Declare, verify and revoke lock the client, re-check `fiscal.authority.manage` and the rendered chain version, and append a successor; a provider 403 appends `ausente_detectada`; `record_certificate_status` stores provider-reported certificate metadata only and marks a foreign CNPJ or unexpected generation `divergente`. Openers `autorizacao_ausente`, `certificado_provedor_vencendo` (30/7/1-day review dates), `certificado_provedor_vencido` and `credencial_divergente`. Workspace section "Autorizações e certificados" (firm-only, flag-gated, private evidence through the scanned staff intake; certificate files refused). | Apply `integrations.0002_authority_records` with `federal_checks` and `document_capture` off: nothing is drawn or evaluated. Recovery is flags off; authority rows are append-only evidence, so the reverse runs only while all three tables are empty and refuses afterwards (forward repair). The reverse never drops the shared `audit_reject_mutation()` function. |
| `AXRM-009` | Client-scoped provider calls, append-only attempt receipts and effect barriers, uncertainty work on AXRM-008's `IntegrationIssue`, a client-less SP-TERMO firm submission journal, and a platform execution epoch. Reservation and dispatch recheck live controls under locks; every external storage/network boundary is outside transactions. Issuance/registration barriers and per-attempt journal replay prevent repeated effects after crashes or restores. Provider usage metrics are added as a prerequisite for restoring individual billed attempts; pricing/caps remain the next todo's mandatory policy hook. | No operation is registered by default; flags stay off and providers paused. Preserve all independent journal prefixes (client, firm, epochs), then run `reconcile_provider_operations` under the global pause. Uncertain effects are not retried. Populated protocol/usage migrations refuse reversal; keep tables and repair forward. Operator, provider-qualification and owner gates stay open. |
| `AXRM-010` | Monthly `ProviderBudget` (tenant) with integer micro-BRL cap, known liability, uncertain exposure and reservations, plus a client-private per-attempt `ProviderReservation` ledger. Reservation locks the budget and refuses (`cota_esgotada`, `cota_ausente`, `tarifa_ausente`) before any I/O against the tenant cap and the platform ceiling; claim and dispatch recheck the held reservation. SERPRO Consulta/Emissao 200/202/403 settle as billed, other statuses release, unknown outcomes stay uncertain until reconciliation. Focus included calls reserve zero only under `focus-plan (PQ-FOCUS-4 pendente)` and stay journaled; Focus documents are metered monthly from captured revisions (the per-call AXRM-009 mapping is removed), Ciencia as estimated `focus_eventos`. Owned `cota_provedor_proxima`/`cota_provedor_esgotada` work at 80%/100%; the owner usage panel shows measured, estimated and missing. | A missing budget row, cap or price disables spend. Recovery = pause the provider and reconcile; usage rows stay append-only. `integrations.0004` reverses only while both budget tables are empty and removes only them; populated balances refuse reversal (forward repair). PQ-FOCUS-4, OD-R1 ceilings and owner activation stay open. |
| `AXRM-011` | Closed A6 operation catalog, bounded Requests transport, per-call HMAC token-broker client, deterministic SERPRO/Focus peers and synthetic fixtures. TLS verification, split timeouts, byte/decompression limits, strict JSON, one credential-free PDF redirect to the documented storage host, and shared two-pass structural credential/body redaction in console/Sentry. | No table or migration; no adapter registration, provider activation or certificate handling. Recovery is a code revert. Attempt 6 awaits independent review; provider qualification, operator and owner gates remain open. See `docs/provider-transport.md`. |

`AXRM-011` adds **144 tests**: **142 pure transport tests** pass in three consecutive
runs, and two exercise fenced finalization and the no-I/O-in-transactions boundary
against the lane database. The combined initial protocol/credential regression
selection passes **15 tests**. The default suite collects **5770 cases**, with
**189 browser cases** separately marked; these are inventory counts, not an
unrun full-suite claim. Full committed-HEAD totals and exits are recorded in
`.evidence/AXRM-011-gates.txt`, using the code-identical `2e2afc4` AXRM-009 baseline
instead of rerunning it (default **5582 passed, 44 skipped**, isolation **777 passed,
44 skipped**, CNPJ pack **29 passed**, browser **189 passed**). The only existing-test
edit is a registration: `tests/scope/test_scope_fidelity.py` lists
`apps/integrations/allowlist.py` among the read-only NF-e and NFS-e references (rows 4
and 10), because the catalog names the received-document GET routes while refusing
issuance.
The initial red has six credential-scrubbing assertion failures; the later
duplicate-key red proves an operation-smuggling case before the parser fix.
Twenty-eight guard mutations fail their witnesses, with matching source hashes
after each isolated campaign. Evidence: `AXRM-011-red.txt`,
`AXRM-011-duplicate-key-red.txt`, `AXRM-011-happy.txt`, `AXRM-011-failure.txt`,
`AXRM-011-json-final-mutations.txt`, `AXRM-011-adversarial.txt` and
`AXRM-011-baseline.txt`. Assets and browser gates are not applicable: this todo
changes no UI, template, CSS, JavaScript or browser test.

`AXRM-011` review fix (gate review attempt 1, needs-fix at `5721789`). Provider
transmission is now bound to the AXRM-009 protocol in code: `begin_io()` records the
dispatch it authorized, `transmit()` accepts only that exact dispatch once and opens
a single-send window for its provider, environment and operation, and
`ProviderTransport.send()` (and the broker client) refuse with
`envio_nao_autorizado` before any connection otherwise. This closes the reviewed
bypass: a direct production `FC-CIE` call with providers paused now opens no
connection. The production console filter redacts log arguments, non-string
messages and header-style pairs before interpolation and scrubs the whole rendered
exception chain with active and collected credential values; the Sentry hook
redacts header pairs. The body read budget now bounds every blocking read instead
of checking only between reads. The rework adds **35 tests** (the transport
selection collects **179**; the default suite **5805**, browser **189**). Existing
test edits: the injected-clock trickle test is replaced by a real elapsed-time test
plus a between-reads budget witness; the misnamed base64/exception-chain test is
renamed to what it checks, with real chain tests added; wire-level tests send
through `permitted_send` in `tests/fakes/provider.py`; broker client tests open a SERPRO
window. Evidence: `AXRM-011-rework-red.txt` (21 assertion failures),
`AXRM-011-rework-red-db.txt`, `AXRM-011-rework-mutations-*.txt`,
`AXRM-011-rework-failure.txt` and the committed-HEAD `AXRM-011-gates.txt`.

`AXRM-011` final-review follow-up. B3: a real-wire regression with the reviewed
byte pattern (0, 20 and 40 s; unmodified 30 s budget) must abort with
`tempo_limite` within 29-31 s; it runs alone through the exclusive gate slot. A real
FC-PDF redirect logged by urllib3 at DEBUG through the production console exposed a
presigned query leak (`AXRM-011-rework2-red.txt`); each wire call now registers its
URL, path-and-query and query as active secrets, a text rule removes presigned query
credentials without a scheme, and known values are replaced before pattern rules.
Tests whose names claimed more than they checked were renamed (Sentry extra field,
Sentry JWT extra, response body/auth header, socket timeout configuration); the
review's three B2 reproductions are kept verbatim as regressions. The transport
selection collects **187** tests; the default suite **5813**, browser **189**. An
exception-note case witnesses the rendered-chain scrub, which argument redaction
alone no longer exercises.

`AXRM-011` review-2 fix (attempt 3). B1: `transmit()` now rechecks, before any
I/O, that the claim still owns the started call (same fence, owner and
epoch, lease not expired) before opening the wire; an expired lease, with or without
another worker's reclaim, raises `StaleClaimError` with zero bytes sent. B2: one
read budget covers response headers and body from the established connection; an
exchange watchdog shuts the socket down when it is spent, replacing the per-read
socket timeout and the private-socket lookup. B3: bytes names and values in
header pairs and mappings are redacted, and the text rule skips a `b` prefix and
no longer re-captures a marker (`[Filtered]]`). B4: a header-pair witness with
space, comma and semicolon values (str and bytes) kills the pair-redaction mutant,
and each condition of the B1 recheck has its own refusing case; an HTTPS header
drip witnesses the TLS connection path. The body-budget timing test now has 1.5 s
headroom. Existing test edits: that test's budget and drip, an elapsed bound on the
injected-clock budget test, and the added bytes cases. The transport selection
collects **211** tests; the default suite **5837**, browser **189**. Evidence: `AXRM-011-fix3-red-pure.txt`,
`AXRM-011-fix3-b2-red.txt` and `AXRM-011-fix3-*.txt`.

`AXRM-011` review-3 fix (attempt 4), placed on main `80c134e` (AXRM-007, 012, 008,
009 and 010). Placement: the firm SP-TERMO path registers its journaled dispatch and
sends through the same consumed, rechecked wire window as client dispatches; a client
dispatch refused before its first byte releases its AXRM-010 attempt reservation
(also after a reconciler quarantined it as uncertain), never a settled bill or a newer
attempt. BLK-1: the Sentry scrubber normalizes field names (bytes, or the SDK's
serialized `b'Authorization'`) before matching. BLK-2: the field rule never captures
an existing marker with its quote or scheme, so scrubbing is idempotent and the
marker renders once. BLK-3: a quoted rendered value is redacted through its closing
quote, so text after a comma, semicolon or space inside it no longer survives.
BLK-4: the recheck returns the call's `lease_until`, the wire permit carries it, and
`open_wire()` consumes the permit and raises `StaleClaimError` once
`now >= lease_until`, the last check before the socket opens; the firm call is left
started for reconciliation. BLK-5 (corrected wording): the recheck is a short
read-only tenant transaction, needed for the transaction-local RLS tenant setting,
committed before any I/O; it takes only the shared execution-epoch advisory lock, no
row lock, and a test proves the connection is idle with no advisory, row or
call-table lock while the adapter sends. A stale claim never reaches the adapter, and
one dispatch transmitted from two threads sends once. Existing test edit: the
wire-test fake's window passes a one-hour lease. A gate follow-up keeps the
reconciliation vocabulary out of the budget release note (scope row 6) and witnesses
that a bytes-keyed mapping value is removed where it appears again. The transport
selection (`test_transport*.py`, `test_token_broker_client.py`) collects **252**
tests; the default suite **6225**, browser **195**. Evidence: `AXRM-011-fix4-privacy-red.txt`,
`AXRM-011-fix4-blk45.txt`, `AXRM-011-fix4-placement.txt` and `AXRM-011-fix4-*.txt`.

`AXRM-011` review-4 repair (owner-authorized attempt 5, 2026-10-25).
The unpublished stack is placed on AXRM-014's reviewed head
`e22065f53df8bdadd9a6b8ac0dcf603d52bdd1bf`, for eventual PR base
`axrm/014-federal-records`. All 17 placement range-diff pairs are unchanged;
there were no conflicts, and both tasks' status records remain. RV4-B3-1:
a complete marker is consumed as a whole rather than trusting a marker prefix.
RV4-B3-2: quoted credential values consume escaped delimiters, including str,
bytes repr and JSON, while retaining subsequent diagnostic context. RV4-B3-3:
Sentry collects nested credential leaves before replacing their labelled field,
then removes echoes regardless of field order; direct short-string collection
is retained. No transport, lease, budget or accepted short RLS transaction design
changes. The original hostile matrix changes from **13 assertion failures,
1 pass** to **14 passed**; the inherited token-shaped Sentry witness has
**3 passed**. The existing privacy suite grows from **88** to **172** tests
(**84 added**, three successful repetitions), and four isolated guard mutants
are killed by named behavioral witnesses. The placed default suite collects
**6475** tests and the browser suite **195**. Full placed-head stage exits are
recorded in `.evidence/AXRM-011-fix5-gates.txt`; red, mutation, range-diff and
sink evidence is retained under `AXRM-011-fix5-*`. Fresh independent review
and PR delivery remain pending; provider qualification, operator and owner
activation gates stay open.

`AXRM-011` review-5 rewrite (owner-authorized attempt 6, 2026-10-26).
Attempt 5 at `f00d7cb` was rejected for deep credential echoes and escaped body
fragments; its DoneClaim and attempt 4's are retained as historical evidence.
Console and Sentry now use one iterative, two-pass walker over mapping keys and
values, lists, tuples, sets and frozensets. Discovery collects sensitive leaves
before replacing whole labelled values and their unlabelled aliases. Rendered
str/bytes/JSON values use an escape-aware tokenizer, complete markers remain
atomic, and replacement is idempotent. Production Sentry discovery runs before
SDK depth/breadth truncation; SDK annotation paths do not become credentials.
Bodies are excluded as whole containers, without discarding adjacent status,
operation, timing or request identifiers. Transport, lease, budget and accepted
short RLS transaction boundaries are unchanged.

The original RV5 repros at `f00d7cb` show **6 nested Sentry failures** and
**36 adversarial assertion failures**. The unchanged matrices now pass:
RV4 **14**, RV3 **133**, RV5 nested **12**, RV5 adversarial **564 checks** and
inherited AXRM-013 **3**. Privacy coverage grows from **172** to **338 tests**
(**166 added**, three successful repetitions). All **79** bytecode-isolated
guard mutations have applied receipts and named failing witnesses: **77** from
the complete campaign and the remaining signed-URL/base64 mutations after adding
real-sink witnesses, with identical production-source hashes. A new assertion
checks body fragments in the actual message rather than random SDK identifiers.
Evidence is retained under `AXRM-011-fix6-*`, including the complete mutation
reconciliation. The default suite collects **6641** cases and the separate browser
suite **195**; these are inventory counts, not passing-suite claims. Read
`.evidence/AXRM-011-fix6-gates.txt` for committed-head STD-GATES exits and execution
totals. Independent review, PR delivery and provider activation remain open.

`AXRM-011` attempt-6 closeout (2026-10-26). The original committed-head run
at `de02654` had one real failure: the existing Sentry integration test received
no transaction events. A second redaction pass parsed a URL whose hostname had
already become `[Filtered]`; the resulting URL-parser exception caused the SDK
to discard the transaction. `247ce01` filters invalid diagnostic URLs and their
echoes without losing the event. Unchanged manual sink QA then exposed scalar
representations corrupting a request ID: `32e2c32` recovers native numbers,
booleans and `None` only on SDK depth-limit annotation paths, while real string
credentials remain subject to redaction. `2db4b6e` isolates only the three new
malformed-URL cases from inherited SDK state before planting their inputs;
their assertions and every pre-existing test remain unchanged.

At `2db4b6e`, the unchanged live Sentry regression passes, the full selected
privacy/security set has **507 passed**, and the exact original group-2 xdist
command has **4556 passed, 51 skipped**. All **15** touched-guard mutants are
applied and killed, with unmutated baselines of **356 privacy tests** and the
live Sentry test passing in separate processes. The **5 failed, 351 passed**
stage is the expected scalar-recovery-removal mutant, not the final suite.
`AXRM-011-fix6c-final-gates.txt` ends with `PIPELINE_FAILED_STAGES=0` and
`PIPELINE_UNIT_EXIT=0`; Ruff, format and Mypy also pass.

The unchanged RV3/RV4/RV5/AXRM-013 matrices pass again. Actual console and
captured Sentry event/transaction QA retain diagnostic identifiers, contain no
planted credentials or body values, and remain idempotent. The **18** added
continuation cases bring privacy coverage to **356**. With the explicitly reused
unaffected group-1 results and six exclusive timing cases, the combined default
inventory is **6608 passing cases and 51 existing skips** (**6659** total), not a
claim of another full-suite invocation. The exact diff proof checks **1745**
unchanged tracked blobs, unchanged existing privacy/security tests, the gate
runner, and production AST outside the two Sentry repairs. Isolation **893/51**,
portal **319**, CNPJ **29**, browser **195**, migrations, build/static checks and
the original **79** structural-redactor mutation witnesses are reused with
their original receipts.

The historical mixed-serial mutation baseline remains recorded as failed
(inherited breadcrumb state; zero mutants applied); no old test was weakened.
Existing editor-only typing findings and the inherited npm advisory remain
outside this repair. Owned attempt-6 containers and completed units are removed,
with empty runtime receipts. Evidence and the replacement DoneClaim are under
`AXRM-011-fix6c-*`; attempts 4 and 5 remain separate. Commit `de02654`'s transport
addition subject misdescribes its redactor rewrite; history is retained rather
than rewritten. Fresh independent review and all activation gates remain open.

`AXRM-011` attempt 7 (RV6-PRIVACY-OVERLAP, owner-authorized 2026-10-26). The
redactor sorted its structural and known-value ranges but skipped any range that
started before the cursor, so a known value crossing into a quoted credential
left its private tail visible at the console and both Sentry configurations.
`b8e7ae1` merges overlapping and adjacent ranges into one union with one
marker; `6be7054` also collects every occurrence of a known value, overlapping
ones included, with a zero-width lookahead, so a value that starts inside an
earlier match is no longer missed and chained windows are removed whole.

The reviewer's attack, unmodified, fails **9 of 9** cases on the attempt-6
product and has **0 of 9** failures with the union. The crossing witnesses
(**9** real-sink cases, exact union, adjacent and separated controls) fail
**11, pass 8** on the attempt-6 product; the overlapping-value witnesses fail
**8, pass 7** on the union alone, with the tail visible, and all pass at
`6be7054`. A union that consumes a closing quote is documented, not changed: no
secret survives, and a second scrub reads the unterminated body to the end and
drops the later diagnostics. Unchanged RV3 **133**, RV4 **14**, RV5 **12** and
**504/48/12**, and AXRM-013 **3** pass again; the privacy module has **378**
passing and the privacy/security selection **529**. Group 1 has **2068 passed**
plus the six exclusive timing cases; the exact group-2 xdist command has
**4556 passed, 51 skipped**; combined default inventory is **6630 passing
cases and 51 existing skips** (**6681**). Ruff, format and Mypy pass, and all
**11** range-union and known-scan mutants are applied and killed (unmutated
baselines pass).

Isolation **893/51**, portal **319**, CNPJ **29**, browser **195**, migrations,
npm and static checks are reused from the reviewer's fresh run at `91e21a7`
behind an exact diff proof: only `apps/integrations/privacy.py` and the privacy
test module differ, **1745** other tracked blobs are identical, and its only
production consumer is `config/settings/prod.py`. The first proof run failed on
my own assertion (two import lines, not one); its log is kept and the corrected
proof passes. Owned lane `axrm-pg-011fix7`/`axrm-redis-011fix7` is removed with
an empty receipt. Evidence is under `AXRM-011-fix7-*`. Independent review and
all activation gates remain open.

`AXRM-011` redaction-scan performance follow-up (review 7 NOTE, fixed before the
wave-2 merge). The attempt-7 lookahead rescanned a long repetitive alternative at
every position: 64 KiB took 3.3 s and 256 KiB 52 s. The known-value scan is now
one Aho-Corasick pass that keeps only merged coverage intervals, so work is linear
in the text plus the known values and every overlapping or self-overlapping
occurrence is still covered before `_union()`; 256 KiB takes 0.17 s. Redaction
semantics are unchanged: a frozen copy of the `329a98c` redactor is compared
byte for byte, including a second scrub, on the rv7 range corpus and seeded
overlapping inputs, and a latency test bounds 256 KiB of repetitive input at one
second under an exclusive slot. There is no input cap, so nothing is skipped or
left unredacted. Mutants of the new scan run in an exclusive slot. Gate totals,
mutation results and the lane receipt are in `AXRM-011-perf-gates.txt` and the
DoneClaim; review and the activation gates remain open.

`AXRM-011` log-filter follow-up. Celery logs every failed task's context under
`extra["data"]`, a provider-body name, so `CredentialURLLogFilter` hid all of it
and remembered even its key names as credentials. The known-value scan then
rewrote the filter's own `exception_arguments` field name and the filter raised
`KeyError` from inside logging, so any failed task could break its caller, and the
task name and id were redacted. The filter now scrubs fields as positional pairs,
withholds a record it cannot scrub instead of raising, and keeps the task name and
id visible while hiding what the task carried or raised. Sentry's Celery
integration ignores that logger, so only the console path needed it. Separately, a
running task's string arguments of eight characters or more become known values
for the length of the run, so an exception message or breadcrumb that embeds them
no longer reaches Sentry; a transformed secret is not recognized. The witnesses
fail on the unmodified filter and every mutant of the new guard is killed.
Evidence is under `AXRM-011-perf-filter-*` and `AXRM-011-perf-args-red.txt`; gate
totals are in the DoneClaim.

`AXRM-009` reuses the supplied code-identical `AXRM-007` baseline at `8a5868c`:
default **5462 passed, 44 skipped**, isolation **740 passed, 44 skipped**.
The collecting red now fails on assertions (**22 failed, 2 passed**, exit 1),
not the inherited annotation error. Fifty-nine new protocol/recovery cases have
three successful runs each, including the positive stale-fence witness.
The real synthetic HTTP peer proves both a successful billed response and a
connection reset after receipt, with no repeated request after reconciliation.
The migration command, grants bootstrap and documented reconciliation CLI have
run on lane 009. STD-GATES on the clean committed head `2e2afc4`
(`AXRM-009-gates.txt`, every command exit 0): Ruff, format, Mypy and migration
drift clean; default **5582 passed, 44 skipped** (+120 against the baseline);
isolation **777 passed, 44 skipped** (+37); CNPJ pack **29 passed**; browser
**189 passed**. No template, CSS or JavaScript changed.
Configuration effects retain barriers even at zero reserved cost. At AXRM-009,
included-plan reads skipped reservation/journal but retained claim/fence; AXRM-010
requires the independent journal for Focus reads too. An incomplete sequence of
journal attempts fails recovery rather than losing earlier billed usage.
The admission tenant lock permits the audit foreign key's `KEY SHARE`: a
concurrent link revocation commits, then the waiting admission is refused,
without deadlocking the existing writer.
A current-fence reply that arrives after its lease expired (or after another
claim quarantined the call) is kept as a metered `late` attempt and the call
stays `incerta`; reconciliation then applies the journaled result and settles
that attempt exactly once (`AXRM-009-late-reply-red.txt`: 2 failed before the fix).
Evidence: `.evidence/AXRM-009-red-assert.txt`, `AXRM-009-happy.txt`,
`AXRM-009-failure.txt`, `AXRM-009-failure-mutation*.txt`,
`AXRM-009-schema.txt`, `AXRM-009-gates.txt`, `AXRM-009-review.txt`.

`AXRM-009` placement on `AXRM-008` (`1963c70`): integrations `0003_call_protocol`
(after `0002_authority_records`) reuses AXRM-008's `IntegrationIssue` through
choices-only AlterFields (reason `chamada_provedor_incerta`, A6 operation
services); an uncertain call joins the one active issue per client and operation
(`aberta` or `em_tratamento`) and every call id stays listed. The firm-level
SP-TERMO submission is journaled without a client (`FirmProviderCall`, append-only
`FirmProviderCallAttempt`, receipts under `provider-journal-firm/`) and rebuilt by
restore replay; actual termo XML submission remains todo 13. A billed reply under
a superseded claim is metered at once, and Focus documents are no longer metered
per request. Red: `AXRM-009-place-red2.txt` (14 failed on assertions). Gates at the
placed head `fe2835b`: targeted **1326 passed, 47 skipped**; default **5822 passed,
47 skipped**; isolation **852 passed, 47 skipped**; static and migration drift,
CNPJ **29**, browser **193**, portal **319** and the asset build reused from the
code-identical `9ebda1b` (`git diff 9ebda1b fe2835b -- apps ops templates static`
is empty); 23 of 23 bytecode-isolated mutations killed by named tests. The
pre-placement baseline at `1963c70` was not measured. Evidence:
`.evidence/AXRM-009-place-*.txt`, `AXRM-009-place-registry-check.txt`.

#### AXRM-010: provider spend

Monthly tenant budgets and per-attempt reservations serialize tenant and platform
caps, retain uncertain exposure, and count SERPRO billed errors. Focus document
units come from captured revision identities per month, not read calls; Ciencia
units are estimated separately. The owner usage panel distinguishes missing,
measured and estimated values. Quota thresholds open owned private work.
See [the integration contract](reference/provider-budgets.md) for configuration,
the capture-producer interface and forward-repair recovery.

Baseline reused with code-identity proof: default **5582 passed, 44 skipped**;
isolation **777 passed, 44 skipped**; CNPJ **29 passed**; browser **189 passed**.
Evidence is in `.evidence/AXRM-010-baseline.txt`.

STD-GATES on the clean committed head `5bc93da` (`AXRM-010-gates-5bc93da.txt`, every
command exit 0): Ruff, format, Mypy and migration drift clean; default
**5662 passed, 46 skipped** (+80 against the baseline); isolation **793 passed,
46 skipped** (+16); CNPJ pack **29 passed**; browser **191 passed** (+2). The two
new skips are the existing per-table composite-key probes reporting that the
tenant-scoped `integrations_providerbudget` has no client composite. No template,
CSS or JavaScript changed. The first run at `c10406d` failed two static scope
scans (banking vocabulary in a budget docstring; an unpinned ORM `"owner"`
relation), fixed in `5bc93da`. Tests first: `AXRM-010-red-acceptance.txt` (33
assertion failures, 1 non-regression pass), `AXRM-010-red-guards.txt`; QA
`AXRM-010-happy.txt`, `AXRM-010-failure.txt`, `AXRM-010-failure-*.txt` (cap,
reservation, release and billed-error mutations each fail their witness),
`AXRM-010-adversarial.txt`, `AXRM-010-usage-{desktop,mobile}.png`.

Review attempt 1 (needs-fix at `532ae93`): restore reconciliation quarantined a
pending read retry and moved its already released reservation back into
uncertain exposure (a 503 read retry plus reconcile: uncertain 0 to 240000).
Fixed in `eff8e1a`: a `known` or `released` attempt is final; only reserved or
uncertain attempts settle, and a reservation rebuilt from the journal takes its
outcome once. Red `AXRM-010-rv1-red.txt` (the reviewer's repro, 1 failed); new
tests also pin the known-guard path (a billed read retry), price overflow, an
unmapped SERPRO billing class and inactive firms in the platform cap. All 24
mutations are killed (the reviewer's 19, one reintroducing the blocker, and the
worker's 4; `AXRM-010-rv1-mutations.log`), each run with an empty bytecode cache
after a same-size, same-second edit reused stale `.pyc`
(`AXRM-010-rv1-stalepyc-proof.txt`). STD-GATES on the clean committed head
`eff8e1a` (`AXRM-010-gates.txt`, every command exit 0, Node v24.13.1): Ruff,
format, Mypy, migration drift and `npm ci`/build with `static/` unchanged;
default **5670 passed, 46 skipped** (+88 against the baseline); isolation
**793 passed, 46 skipped**; CNPJ **29 passed**; browser **191 passed**.
`AXRM-010` placement on `AXRM-009` (`66df74b`): the budget migration is now
integrations `0004_provider_budgets` (after `0003_call_protocol`), with a choices-only
AlterField adding `cota_provedor_proxima` and `cota_provedor_esgotada` to AXRM-008's
`IntegrationIssue`; quota work joins the one active issue per client, reason and A6
operation (blank service for Focus document overage) through the same lock, savepoint
and join as call-uncertainty issues, owned by AXRM-008's client owner. New tests prove
a billed late reply racing two late finalizes and two reconciles is metered and
settled once under the call-keyed budget usage, and that concurrent refusals converge
on one owned issue. Registries list each 007-010 table once; tenant tables 77 + 2 = 79.
Gates at the placed head `309e350` (`AXRM-010-place-gates.txt`, every command exit 0,
Node v24.13.1): Ruff, format, Mypy, migration drift and `npm ci`/build with `static/`
unchanged; default **5924 passed, 49 skipped** (1532 + 4392); isolation **868
passed, 49 skipped**; portal **319**; CNPJ **29**; browser **195**. 24 of 24
bytecode-isolated mutations killed; the populated two-tenant `0004` round trip
refuses a populated reverse and removes only its own 15 objects when emptied.
Evidence: `.evidence/AXRM-010-place-*.txt`, `AXRM-010-place-registry-check.txt`,
`AXRM-010-place-range-diff.txt`.
Provider qualification (PQ-FOCUS-4), the OD-R1 ceilings and owner activation
gates remain open.

The pre-change baseline for `AXRM-007` is the independent `AXRM-006` rv4 measurement
at `ecef4e0`, code-identical to `559bb5a`: default **5332 passed, 41 skipped**
(1227 + 4105), isolation **724 passed, 41 skipped**, CNPJ **29**. At the `AXRM-007` code,
under the gate lock, the default suite passes **5459, 44 skipped** (`test` 1294,
`test-rest` 4165), isolation **739 passed, 44 skipped** (the three new skips are schema
facts: `ProviderConnection` has no parent foreign key and `ClientProviderLink`'s
composites are tenant-paired, so the client-swap probe does not apply), CNPJ **29** and
browser **189** (four new panel cases). The targeted selection (349 tests) passed in
each of three runs. Lint, format, Mypy, migration drift, system checks and the static
rebuild are clean. On a populated lane database `integrations.0001` reverses while
empty, refuses once a row exists and keeps its rows, and `authz.0009` restores the
matrix to 31 rows / 186 cells and back to 36 / 216; a raw `UPDATE` cannot move a set
capture start month. Removing `DenyPortalAccess` fails the portal-decision meta-test;
a secret value in `secret_ref` is refused by the validator and the database. Existing
tests changed only as registrations (matrix counts and published rows, flag registry,
isolation inventories, portal NONE lists, stash slug count, R1 rollback slug set,
scope pins). Evidence: `.evidence/AXRM-007-red.txt`, `AXRM-007-baseline.txt`,
`AXRM-007-gates.txt`, `AXRM-007-migration.txt`, `AXRM-007-happy.txt`,
`AXRM-007-failure.txt`, `AXRM-007-adversarial.txt`, `AXRM-007-teardown.txt`.

Review fix (`AXRM-007` review blocker B1): the reverse of `authz.0009` deleted a
regional capability that existed before the migration, with its role cell and two
firms' user grants. The forward now records each capability and role cell it creates,
and the earlier label or level of each one it changes, in the platform ledger
`authz_capabilityseedrow` (non-tenant, `-- PORTAL_NONE:` and `PORTAL_DENIED_TABLES`,
isolation inventories), which 0009 creates and drops itself. The reverse restores what it
changed, deletes only what it created, and refuses while a firm holds a grant on a
capability 0009 created. `authz.0002` no longer writes those five slugs: it seeds the
current matrix, so on a fresh database it had created them before 0009 ran, leaving
0009 nothing to record (deployed databases applied 0002 before the matrix named them).
The idempotency test now replays every authz seed in order. Suites at `8a5868c`, under
the gate lock: default **5462 passed, 44 skipped** (`test` 1294, `test-rest` 4168: the
two new rollback tests and the ledger's portal case), isolation **740 passed,
44 skipped**, `tests/authz` **417**, and the targeted selection (781 tests) in each of
three runs. Lint, format, Mypy and migration drift are clean. Browser, CNPJ and portal
were not rerun. As `app_migrator` on a fresh lane, `authz.0009` again restores 31 rows /
186 cells and back to 36 / 216. A populated two-tenant round trip keeps a pre-existing
`fiscal.checks.run` with its label, role cells and both firms' grants while removing the
four capabilities 0009 created, and the reverse refuses once a firm is granted one of
those. Evidence: `.evidence/AXRM-007-red-b1.txt`, `AXRM-007-red-b1-fresh.txt`,
`AXRM-007-migration-b1.txt`, `AXRM-007-b1-*.log`, `AXRM-007-gates.txt`.

`AXRM-012` adds `portal_visible` to documents and obligations without changing the
legacy database default or any existing row. Both restrictive portal policy clauses
fold the flag into the anchored client comparison
(`client_id = CASE WHEN portal_visible THEN <client> END`, the `cycles_exception`
precedent), so a hidden row is unreadable and uninsertable for `app_portal` at the
policy level. `cycles_documentintake` reads follow the parent document the same way:
the USING client comparison holds only while the parent document is visible to
`app_portal`, so quarantine metadata of a hidden original is not readable (its WITH
CHECK stays client-only, so the composite FK still refuses a foreign parent). No portal grant,
view filter or `roles.sql` policy statement is added; `roles.sql` documents each
decision beside its grant. Document hash uniqueness includes visibility, so a client
can upload bytes already held privately and gets a separate visible row.

Acquisition origins distinguish portal and staff uploads from provider capture and
federal acquisition (existing rows backfilled from `client_upload`); only provider
origins may omit a human uploader, and they require a receipt reference. The portal
INSERT trigger refuses forged origins, private rows, missing human attribution and
receipt metadata. `ingest_system_document` creates a private, actor-less original
and a quarantined intake through the same capped, sniffed intake code as portal
uploads, which the existing scan sweep processes. `staff_upload` is
portfolio-scoped and private by default (the plan proposed a `True` default for an
existing staff path; none exists, so the new function fails closed). Visibility stays
an explicit later decision for an approved publication.

The pre-change baseline for `AXRM-012` at `559bb5a` is **5332 passed, 41 skipped**
(group 1: 1227; group 2: 4105 passed, 41 skipped). At `28b5f39` the default suite
passes **5382, 41 skipped** (group 1: 1227; group 2: 4155 passed, 41 skipped; the 50
new tests), isolation **743 passed, 41 skipped**, `tests/portal` **319** with no portal
test changed, CNPJ **29** and browser **185**. The new targeted set passes **69** in
each of three runs. Every new predicate is mutation-proven: 26 of 26 deliberate breaks
(each policy side, each trigger condition, both checks, the hash unique, the dedup
filters, the ingest and staff guards, each reverse-guard clause and the backfill) fail
a named test. An `app_migrator` round trip on a populated two-tenant database
backfills origins, keeps every row visible and identical through reverse and
reapply, and refuses the reverse once a provider-origin row exists. Lint, format,
Mypy and migration drift are clean. The generic isolation seeder registers the now
nullable uploader in its constraint table, so its document stays a human upload.
Evidence: `.evidence/AXRM-012-gates.txt`, `AXRM-012-red.txt`,
`AXRM-012-red-corrective.txt`, `AXRM-012-mutations.txt`, `AXRM-012-happy.txt`,
`AXRM-012-failure.txt`, `AXRM-012-invariants.txt`, `AXRM-012-adversarial.txt`,
`AXRM-012-final2-migration-cli.txt`.

Review fix (`AXRM-012` attempt 1, independent review reject): a hidden obligation or
document stayed readable one hop away. A firm payment fact (`POST /ciclos/<id>/`,
`record_staff_fact`, the receipt materializer), a request bound to the obligation
(`create_request`, `send_requests`, then `/solicitacoes/<id>/`) and links to a hidden
document or request were readable by `app_portal`, and the vault accepted a hidden
obligation id. `obligations.0023` now also folds the parents into the USING client
comparison of `cycles_evidencerequest` (its obligation), `cycles_evidencelink` (its
document and request) and `obligations_sourceobservation` (its subject and evidence
link); each subquery runs under the parent's own portal policy, so the chain reaches
the request's obligation and the link's document. WITH CHECK stays client-only, so
the composite foreign keys still refuse a foreign parent first. On the write side a
client-facing request cannot be bound to, or sent with, a private obligation, and a
vault upload naming an obligation that is not this client's and visible is a 404
before any byte is read. Firm facts about private obligations remain allowed and
private. Nothing private before the fix becomes visible; the reverse restores the
original predicates of all six tables behind the same forward-repair guard.

Suites after the review fix at `5ab4c32`, rebased on `AXRM-007` (`8da6d30`): the default
suite passes **5523, 44 skipped** (group 1: 1294; group 2: 4229 passed, 44 skipped),
isolation **770 passed, 44 skipped**, `tests/portal` **319** with no portal test
changed, CNPJ **29** and browser **189**; the targeted set passes **80** in each of
three runs. Collected counts over `8da6d30` grow by exactly the new `AXRM-012` tests
(+30 isolation, +61 default, +0 portal). Against the pre-fix product code the seven new
hidden-case tests and the fold coverage test fail. All 35 deliberate breaks fail a named
test, including one per new clause (request obligation, link document, link request,
observation subject, observation link) and one per new write guard (binding, send, the
vault check and its visibility filter). The `app_migrator` round trip now checks all six
folded policies forward, the original predicates after reverse, the folds after reapply
and the refused reverse once a provider-origin row exists. Evidence:
`.evidence/AXRM-012-gates.txt`, `AXRM-012-rv1-red-smoke.txt`, `AXRM-012-mutations.txt`,
`AXRM-012-failure.txt`, `AXRM-012-invariants.txt`, `AXRM-012-happy.txt`,
`AXRM-012-adversarial.txt`, `AXRM-012-rv1-reference-audit.txt`,
`AXRM-012-rv1-collection-delta.txt`, `AXRM-012-rv1-migration-cli-classification.txt`.

Review fix 2 (`AXRM-012` attempt 2, independent review reject): a document whose own
flag was visible stayed readable, listable and downloadable, with its intake, while a
firm-side writer had bound it to a private obligation. `obligations.0023` now folds the
bound obligation into the document's USING comparison (`obligation_id IS NULL` or a
visible obligation), and intakes, links and observations inherit it through their
existing document subqueries. The relational class was enumerated from the model graph
and matches the reviewer's live catalog query: eight portal-readable foreign keys reach
a private-capable row, each is now folded, and a catalog test fails when a new one
appears unregistered. The one self-reference, `sourceobservation.supersedes_id`, cannot
be folded by a subquery on its own table, so a claim chain is readable only while every
member's subject and evidence link are: a SECURITY DEFINER function on the cycles `0014`
pattern (ambient tenant and client checks, empty search_path, EXECUTE for `app_portal`
only) lists the chain's members, and the policy checks each under the portal's own
policies. Hiding the whole chain also keeps a superseded claim from becoming the
portal's current claim when its successor is private. The document hash key now includes
the bound obligation (nulls not distinct), so a client upload of bytes held in an
obligation-private document is a separate visible row instead of a collision, and the
reverse also refuses while that wider key holds rows the original key would reject.
Visible-parent and null-parent rows read as before.

Suites after review fix 2 at `6c8e90a`: the default suite passes **5536, 44 skipped**
(group 1: 1294; group 2: 4242 passed, 44 skipped), isolation **782 passed, 44 skipped**,
`tests/portal` **319** with no portal test changed, CNPJ **29** and browser **189**; the
targeted set passes **93** in each of three runs. Collected counts over `85493e7` grow
by exactly this fix's 13 new tests (+12 isolation, +1 migration). Against the reviewed
product code 10 of them fail, among them the reviewer's own B1 reproduction, while every
visible-parent and null-parent control passes. All 43 deliberate breaks fail a named
test, including one per new clause: the document's obligation fold and its null escape,
the chain fold and its subject and link checks, the chain function's ancestors,
descendants and client scope, the widened hash key and the reverse guard's duplicate
check. The `app_migrator` round trip, run on a freshly reset database, also checks that
the chain function exists forward, is dropped on reverse and returns on reapply.
Evidence: `.evidence/AXRM-012-gates.txt`, `AXRM-012-rv2-red-smoke.txt`,
`AXRM-012-mutations.txt`, `AXRM-012-failure.txt`, `AXRM-012-invariants.txt`,
`AXRM-012-happy.txt`, `AXRM-012-adversarial.txt`, `AXRM-012-rv2-reference-audit.txt`,
`AXRM-012-rv2-collection-delta.txt`.

`AXRM-008` records each client's authority as append-only `AuthorityRecord`
revisions (trigger for every role, SELECT/INSERT for the runtime role): a successor's
composite foreign key repeats (tenant, client, service), versions are unique per
service with version 1 the root, and every row names a person or the refusing provider
call. Catalog CHECKs bind each service to its kind and code (SITFIS 00002, PAGTOWEB
00004, PGMEI `nao_exigida`, client authorizations for capture and Ciência). A human
declaration is shown as declared, never verified; a named verifier makes it
"Verificada por ... em ..." with the `manual` badge, never `confirmed`. After a
revocation, expiry or provider-detected absence a new declaration waits in
`verificacao_pendente`. `authority_for` is the single admission answer for todo 9:
operations outside the implemented A6 set are `unknown`, expiry comes from the injected
date, and capture also requires a current, matching, non-divergent certificate on every
active Focus link. Problems become owned work: the registered cycle exception when the
client has an open cycle, otherwise one active `IntegrationIssue` per reason and
service (partial unique); a usable verification or a healthy certificate report
resolves the matching issue, never a source observation. `FirmAuthorization` carries
exactly the plan's fields for todo 13's termo; its tenant-paired document key needs a
new `(tenant_id, id)` anchor on `obligations_document`, exempted in the client-column
uniqueness meta-test with the `invite_tenant_id_uniq` argument. The credential state
machine gains the provider-sync moves `ausente -> divergente` and
`confirmacao_pendente|rotacao_pendente -> vencendo|vencida`.

The pre-change baseline for `AXRM-008` is the `AXRM-012` rv1 measurement at `5ab4c32`
(this branch's base, code-identical): default **5523 passed, 44 skipped** (1294 +
4229), isolation **770 passed, 44 skipped**, CNPJ **29**. Before implementation the 46
new acceptance cases failed on assertions. The 62 new tests pass in each of three runs,
and 46 of 46 deliberate breaks (every service guard, view guard, sweep filter, opener
hook, migration trigger, foreign key, CHECK and unique) fail a named test. Existing
tests changed only as registrations (portal NONE lists, append-only list, isolation
inventories and seeder, the tenant-table count 69 -> 72, the credential-route
exemption, scope pins for the received NF-e/NFS-e capture labels, the client-column
uniqueness exemption and the regional read-budget world, which now renders every
authority row inside the unchanged 25-query budget). At the `AXRM-008` code
(`b52657e`), under the gate lock, the default suite passes **5629, 45 skipped** (+106 tests; the one new skip is a schema fact: `FirmAuthorization`
has no client column, so the client-swap probe does not apply), isolation **796
passed, 45 skipped**, CNPJ **29** and browser **191** (two new journey cases at
1440x900 and 390x844). Lint, format, Mypy, migration drift and the static rebuild are
clean. Evidence:
`.evidence/AXRM-008-red.txt`, `AXRM-008-baseline.txt`, `AXRM-008-flaky.txt`,
`AXRM-008-mutations.txt`, `AXRM-008-happy.txt`, `AXRM-008-failure.txt`,
`AXRM-008-adversarial.txt`, `AXRM-008-gates.txt`, `AXRM-008-teardown.txt`,
`AXRM-008-screens/`.

Review fix (`AXRM-008` attempt 1, independent review needs-fix): three guards had no
test that failed without them -- the declaration transition guard, the reported
credential transition guard and the view-level `require_can`. Checking the
declaration guard against A7.3 found a real gap as well: a pending declaration whose
validity lapsed before anyone verified it could be neither verified, revoked nor
declared again. A revision whose validity ended (active or pending) now accepts a
new declaration, which is again pending and unusable until verified; every other
re-declaration over a current or pending revision is still refused with no row and
no event. The credential guard's unreachable error branch is removed (every state
but `divergente` may move to `divergente`). New tests re-declare over pending,
active, expiring and revoked revisions with and without a verifier, report each
forbidden certificate move (`vencendo`/`vencida` -> `ativa`, `vencida` ->
`vencendo`) and its drift work item, and hit the authority routes over HTTP as each
role of the six-role matrix (staff refused at the view gate with its recorded
refusal before any upload; client roles and the platform admin refused with no
rows; owner and ops reach the service). At `da114a5` the default suite passes **5650,
45 skipped**, isolation **796 passed, 45 skipped**, CNPJ **29** and browser **191**; the
83 AXRM-008 tests pass in each of three runs, and a bytecode-isolated mutation run
kills all 64 mutants (the 47 worker and the review's 17). Evidence:
`.evidence/AXRM-008-rv1-red.txt`, `AXRM-008-rv1-green-flaky.txt`,
`AXRM-008-rv1-mutations.txt`, `AXRM-008-rv1-gates.txt`.

Review fix 2 (`AXRM-008` attempt 3, independent review needs-fix), placed on the final
`AXRM-012` head `c42a9bf`: the only rebase conflict was this file, and every registry
and count in the auto-merged `ops/sql/roles.sql`, `tests/conftest.py` and isolation
inventories registers both todos' tables exactly once (tenant tables 72 = 69 + 3). A
lapsed pending authority now shows "Vencida sem verificação" with the renewal form
instead of a verification action that answered 409; the workspace uses the
declaration service's own date rule, and the replacement still waits for
verification. Client owners, client collaborators and the platform admin were
refused by the tenant middleware before any view ran, so no refusal was recorded;
when that refusal targets a `require_can` view the middleware now records the same
durable `DENIED` event the view would have, for the resolved firm, and still admits
nothing. Every denied role of the six-role matrix writes exactly one refusal per
request, and a gated page reached without a seat still refuses without client data. At
`c931570` the default suite passes **5666, 45 skipped** (group 1: 1380; group 2: 4286
passed, 45 skipped), isolation **808 passed, 45 skipped**, `tests/portal` **319**, CNPJ
**29**, browser **193** and the migration round trip **15**; static checks and the
static rebuild are clean, and a bytecode-isolated run kills all 70 mutants (53 worker,
six of them for the new guards, and the review's 17). Evidence:
`.evidence/AXRM-008-rv2-red.txt`, `AXRM-008-rv2-green-flaky.txt`,
`AXRM-008-rv2-registry-check.txt`, `AXRM-008-rv2-mutations.txt`,
`AXRM-008-rv2-gates.txt`, `AXRM-008-rv2-screens/`.

### Wave 2: federal checks

| Todo | Engineering delivery | Rollout and recovery |
| --- | --- | --- |
| `AXRM-014` | Client-scoped, firm-only federal records in `obligations.0024` and repair `0025`: `FederalConsultation` (receipt: kind, scope digest, range, `consulted_at`, outcome `encontrado`/`nada_localizado`/`sem_procuracao`/`falha`/`cota_esgotada`/`incerto`, opaque `provider_ref`, environment), append-only `FederalPayment` (unique per client `numero_documento`, Decimal(14,2) amounts, consultation and nullable matched obligation) and `FiscalSituationReport` (A7.3 states, at most three emission attempts, private original protected against attachment, reassignment and later visibility changes). `record_payment_consultation` resolves the actual tenant/client-scoped SERPRO payment call, rejects inconsistent metadata, derives provenance and time from successful persisted finalization, and requires production plus `PROVIDER_REAL_DATA_APPROVED` for a matched payment's verified `official_api` observation. Empty results remain receipts only; `source_subjects` includes payment-derived obligations. Fresh independent review remains required after repair. | New tables start empty and no provider slice writes them yet (todos 15-18). Reverse while empty; once any receipt exists the scope/privacy repair refuses reversal (forward repair, flag `federal_checks` off). Observations stay append-only. No provider, operator or owner gate closes here. |

#### AXRM-014: federal consultation records

Baseline at `80c134e` (`AXRM-014-baseline-r2.txt`; the first baseline log was
overwritten by a duplicate unit launch and is not counted): default
**5924 passed, 49 skipped** (1532 + 4392). Tests first: `AXRM-014-red.txt` at
`80c134e` with only the new tests and registry edits (8 failed, 34 passed,
9 errors; every error is the missing module, models or tables). This does not
establish historical assertion-first red for each payment behavior. The later
26 killed mutations establish behavioral sensitivity, not that earlier sequence
(`AXRM-014-mutations.txt`; 25 fail on an assertion or `DID NOT RAISE`, the
float-to-Decimal one on the guard's own `ValidationError`), each with a private
bytecode prefix and a fresh test database, and the unmutated control passes.
The malformed-input probe found
`"1e400"` escaping as `InvalidOperation` at `41fe249`
(`AXRM-014-adversarial-malformed-r1.txt`), fixed in `71065a7`.

STD-GATES on the clean committed head `71065a7` (`AXRM-014-gates.txt`, every
command exit 0, Node v24.13.1): Ruff, format, Mypy, system checks (dev, test,
prod), migration drift, effective-role RLS check, `npm ci`/build with `static/`
unchanged; default **6048 passed, 51 skipped** (1532 + 4516; +124 against the
baseline); isolation **893 passed, 51 skipped**; portal **319 passed**; CNPJ
**29 passed**; migration round trips **11 passed**; browser **195 passed**.
Tenant tables 79 -> 82, each new table registered exactly once in every isolation
registry (`AXRM-014-registry-check.txt`). QA: `AXRM-014-happy.txt` (production
DAS -> payment, verified official observation, coverage `verified`),
`AXRM-014-failure.txt` (trial -> records, no observation, coverage unchanged;
empty -> `nada_localizado`), `AXRM-014-repeat-{1,2,3}.txt`,
`AXRM-014-adversarial.txt`, `AXRM-014-teardown.txt`.

#### AXRM-014 repair round 1

The independent review of `3d68e3d` rejected three stored invariants. Repair
`0025_federal_record_guards` adds the durable consultation scope digest and a
database-generated constant-False report key with a composite foreign key to
the document's client/id/visibility. The existing attachment trigger stays in
place; the native key also refuses later visibility updates and protects both
transaction orderings without relying on a trigger's cross-table snapshot or
effective-role visibility.

The writer locks the actual tenant/client-scoped `ProviderCall` before domain
writes. Environment, request digest and SERPRO/SP-PAG/read metadata must agree;
a successful result requires `sucesso` and non-null `finalized_at`. Official
observations still require the real-data approval gate. The opaque reference
remains storage, not proof by itself. New receipts retain the canonical request
digest; legacy receipts receive an explicitly namespaced digest of their retained
scope, not an invented historical provider request. Populated reverse migration
refuses loss of either scope or privacy protection.

Repair baseline: **81 passed** at the reviewed head. Assertion-level repair red:
**15 failed**, no collection/setup errors, in
`AXRM-014-fix1-20261025T161703Z-red.txt`: twelve call-provenance refusals, two
actual post-attachment visibility updates under runtime and owner roles, and the
missing durable scope field. The latter is a schema assertion; the named reload
test and scope-write mutation establish the later persistence contract.
The repaired control and two repetitions each passed **121 tests**, including
attachment/reassignment races in both orders under READ COMMITTED and REPEATABLE
READ. The inherited mutation pipeline exited 1: six application markers were
missing and one failure-with-payments mutant survived a masked test. Those
attempts are preserved and are not counted as successful kills. The failure test
now uses a valid refused-before-dispatch scope rather than an invalid call id;
the finalization-clock test uses an explicitly different fixed input.
The retained `20261025T165150Z` recheck exited 0: the six marker cases, the
failure-with-payments case and the changed clock case all produced their named
behavioral failures with application markers and unchanged source. Together with
the earlier 36 valid kills this establishes **43 unique killed mutants**, not
44: the clock case was rechecked, not added. Both changed tests passed three
times. No completed successful run was relaunched during recovery.

Downstream notes from the review remain assigned to their original todos:

- Todo 15 owns PAGTOWEB paging and real field normalization, including `tipo` as
  a domain object, `receitaPrincipal` as a Receita object, DAS code `09`, and ISO
  period dates. The flattened synthetic parser fixture is not qualification.
  Deterministic MEI/DAS/month matching, partial-page budgets and receipt retrieval
  are also not certified by this record-layer repair.
- Todo 16 owns protocol scheduling, scan-before-review, reviews, exceptions and
  report orchestration. Private originals remain firm-only.
- Cycle-aware todo 15/16 services must emit the A7.8 federal events with
  `actor=NULL`, only when the matching cycle exists, in the domain-finalization
  transaction. The record helper does not implement that orchestration.
- Same-reference replay retains the original payload. Callers own canonical
  request/scope construction and its range mapping; no new payload-conflict
  contract is claimed here. Existing seven-day official-api freshness remains.

Repair evidence is retained separately under `.evidence/AXRM-014-fix1-*`.
The committed repair head still requires full standard gates, manual/adversarial
QA and cleanup before fresh independent review; no approval is claimed here.
#### AXRM-013: SERPRO adapter and authorization

| Todo | Engineering delivery | Rollout and recovery |
| --- | --- | --- |
| `AXRM-013` | Allowlisted SERPRO adapter, isolated memory-only mTLS token broker, externally signed termo intake and submission, and the fake/trial qualification registry and command. Client and firm calls use the existing claim, journal, fence and budget protocol; only a complete first 401 permits one fresh attempt. Firm uncertainty and missing procurador authority have owned client-less work (`FirmIntegrationIssue`), with explicit zero-price attempt reservations (`FirmProviderReservation`). Contratante PEM secrets mount only in the broker; tokens never enter persistent state. | Default environment remains `fake`, flags off and providers paused. Apply `integrations.0005` and `0006`; empty tables reverse, while populated work or reservation history refuses reversal and requires forward repair. Recovery is provider pause plus dropping the broker's process memory. OG-R2, OG-R3, OG-R5 and PQ-SERPRO-4 remain open; no real provider request or independent-review approval is claimed. |

`AXRM-013` preserves the existing implementation and its evidence, rebased onto
todo 11's docs-only final head `1a5564b`. Recovery adds explicit platform-query and
firm-membership scope declarations and two witnesses for exhausted firm retries;
the production retry predicates are unchanged. The initial tests-first run has
**129 assertion failures, zero errors**. The queued baseline observed newly authored
tests and stopped on their lint findings, so no pristine baseline suite total is
claimed.

The preserved full default runs record **1943 passed** in group A and **4421
passed, 51 skipped, two failed source guards** in group B. Recovery passes all
**35** scope guards and all **5** qualification cases; all **7** firm retry cases
pass in each of three consecutive runs. Replacing the two failed guards and adding the two
new witnesses gives **6368 passing cases covered, 51 skipped**. This combines the
original complete runs with the verified delta; it is not a new full-suite
invocation. Apart from this status document and the three scoped recovery files,
all tracked inputs are byte-identical to `e51c43e`.
Ruff, format and Mypy pass on the recovery code.

Preserved unchanged gates: isolation **884 passed, 51 skipped**, portal **319**,
CNPJ **29**, browser **195**, migration round trip **10**, system checks, migration
drift, effective runtime-role check, npm build and static diff. Tenant tables grow
from **79 to 81**, with each new model in the populated behavioral sweep, portal
NONE registries and composite-parent inventory. All **32** original catalogue
mutations are killed, plus two independent firm retry variants, with isolated
bytecode and restored-source proof. Happy/failure API and command QA and all nine
adversarial classifications are recorded; owned containers and new runtime
directories are removed. Independent review is the next orchestrator step.
Evidence: `.evidence/AXRM-013-recovery-gates.txt`,
`AXRM-013-recovery-mutations.txt`, `AXRM-013-registry.txt`,
`AXRM-013-happy.txt`, `AXRM-013-failure.txt`, `AXRM-013-adversarial.txt` and
`AXRM-013-teardown.txt`.

`AXRM-013` repair round 1 addresses only the three new independent-review
blockers: the application build excludes the default `ops/secrets/` sources;
competing billed-403 work openers join the same unique issue instead of raising;
and a malformed HTTP-200 termo response opens owned unresolved firm work before
the symbolic parse failure propagates. Its HTTP receipt remains final, no
authorization is accepted, and the call is not reopened or resent. Three new
regression cases cover real Docker COPY/export, two barrier-controlled database
sessions and the malformed signed-termo submission. Final committed-head counts,
isolated mutation results and cleanup receipts live in the
`.evidence/AXRM-013-fix1-*` artifacts.

The first repair gate campaign at `e6f0d5a` passed 1,947 default group-A cases;
group B recorded 4,423 passed, 51 structural skips and one source-guard failure:
the new ORM defaults key `"owner"` was mistaken for a tenant-role literal.
The exact-source status-guard registration now identifies that field key; no
assertion, budget or status policy is relaxed. The original failed gate log is
preserved, and refreshed committed-head receipts are recorded separately.

**Custody remains blocked on the parent. `AXRM-011` [~], `AXRM-013` [~]: inherited
013-I1/I2/I3 are still open.** Marker-prefixed and escaped credentials can leak
through the inherited telemetry code, and nested Sentry credentials can survive
in an unlabelled echo. This round does not change `privacy.py` or
`config/settings/prod.py`, certify aggregate custody, restack onto todo 14 or
close independent-review/operator gates. The owner's separately authorized
AXRM-011 attempt 5 has not been absorbed or independently confirmed here.

## Axorim pivot

The product direction changed on 2026-09-27. The local-only plan
**`axorim-pivot-r1`** governed the R1 work delivered and merged on 2026-10-15;
the current plan is the regional MVP above. Like every plan since the exclusion,
it lives under `.omo/plans/` and is never committed. "Axorim" is an internal working
name. The repository, the README headline and customer-facing copy stay "app-mei"
until the name is cleared.

- **Ids.** The plan has 29 original numbered todos in five waves, the added
  `AXR1-030` certificate guard, `AXR1-031` customer-facing label repair,
  `AXR1-032` support-authority repair, `AXR1-033` contact-invitation binding,
  `AXR1-053` tenant notice authorization, `AXR1-048` import-only invitation
  routing, `AXR1-039` automatic reminder sweep, `AXR1-049` logout recovery after denial, `AXR1-050`
  support audit actor, `AXR1-044` ignored late observations shown as history, `AXR1-047`
  discoverable setup after import, `AXR1-043` linked workspace sections, `AXR1-040` unsaved-changes guard, `AXR1-042` portal next action, `AXR1-045` portal contrast, `AXR1-046` reopening disclosure, `AXR1-036` cycle-detail query budget, `AXR1-035` workspace query budget, `AXR1-037` constrained Hoje latency, `AXR1-038` every-model isolation, `AXR1-054` readable refusals, `AXR1-034` named-read and interactive performance evidence, `AXR1-055` saved dates in date inputs, `AXR1-056` portal empty-state contrast, `AXR1-058` completion execution refusal, `AXR1-059` refusal pages way out and product 404, `AXR1-060` portal next action for awaiting publications, `AXR1-061` email metering states, `AXR1-062` R1 client flows in pt-BR, `AXR1-063` upload-refusal ink, `AXR1-064` refused playbook creation form, `AXR1-057` evidence export with unscanned originals, and a final verification wave.
  Required follow-up `AXR1-041` is delivered and waits only for its integrated top-of-stack CI run; repair items
  `AXR1-055` to `AXR1-064` follow it in the stack; optional proposals 051/052 are not
  accepted implementation scope. Todo N is cited
  as `AXR1-NNN` (todo 1 is `AXR1-001`) in commit bodies and in
  evidence filenames of the form `.evidence/AXR1-NNN-<kind>.txt`.
- **Delivery.** One open pull request per todo in a linear stack: the first is
  based on `main`, and each later branch and PR is based on the preceding todo's
  branch. No merges or force pushes; merging awaits the user's instruction
  because it deploys to the pilot host. Every new surface stays behind a
  per-tenant feature flag that's off by default. Each pull request updates the
  progress line below and the suite baseline.
- **Records.** `docs/product/pivot-supersession.md` says which earlier restrictions are
  superseded and which still bind. ADRs 0002 to 0007 in `docs/decisions/` hold the
  architecture decisions, and `docs/product/vocabulary.md` holds the status words.
- **Pilot obligations stay separate.** The v2 plan records F1-F4 complete and
  pilot start authorized on 2026-09-26 for `a962173` only. R1's delivery does not
  carry that authorization forward: the owner's OD-02 decision must settle
  carry-forward or a re-run before OG-02 can close for the current release.

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
audit streams. `AXR1-007` (in-product onboarding behind `import_r1`: create/edit
with a tenant-scoped duplicate check and versions, engagement, tax-profile,
contact and assignment editors, contact portal invites issued under lock and
mailed after commit, and the CSV/XLSX portfolio import with plan, TenantTask
commit, retry and dependents-aware rollback; `ImportBatch`/`ImportRow`,
`clients.0015`) is recorded on the T6 stack, using the S8.1 `clients.manage` and
`clients.import` capabilities and keeping the MEI flag in agreement with the tax
timeline on every new write path. Tests: `tests/clients/test_import_wizard.py`,
`tests/clients/test_onboarding_screens.py`,
`tests/clients/test_contact_invite_delivery.py`,
`tests/clients/test_onboarding_flag_agreement.py` and
`tests/browser/test_onboarding.py`. Evidence: `.evidence/AXR1-007-red.txt`,
`-happy.txt`, `-failure.txt`, `-adversarial.txt`, `-migration-reversal.txt`,
`-gates.txt`, `-fix-1.txt` through `-fix-4.txt`, `-fix-6.txt`, `-fix-gates.txt`,
`-fix6-gates.txt`, `-placement.txt`, `-placement-migration.txt`,
`-placement-gates.txt` and the eight onboarding/import screenshots. `AXR1-008`
(versioned, typed playbooks behind `cycles_r1`: `Playbook`,
`PlaybookVersion`, `PlaybookStep` with draft-only mutation,
`expected_version` revision checks, persisted-parent reparent guards and a
preview-signed publish; the MEI mensal and Coleta mensal system packs installed
per-tenant under a signal, converging under concurrent install; targeting
driven by `TaxProfile`/`Engagement` snapshots, never the legacy flag; gated on
`playbooks.manage`; `cycles.0001`) is recorded on the T7 stack. Tests:
`tests/cycles/test_playbook_versioning.py`, `test_adversarial.py`,
`test_playbook_http_conflicts.py`, `test_step_formset.py` and
`tests/browser/test_playbook_editor.py` plus `test_playbook_publication.py`.
Evidence: `.evidence/AXR1-008-red.txt`, `-happy.txt`, `-failure.txt`,
`-adversarial.txt`, `-gates.txt`, `-fix-1.txt` through `-fix-4.txt`,
`-fix2-1.txt` through `-fix2-3.txt`, `-fix2-mutations.txt`, `-fix2-repeat.txt`,
`-fix2-review.txt`, `-fix2-adversarial.txt`, `-fix2-gates.txt`,
`-placement.txt` and the editor screenshots. This verified a populated-schema
round trip: `migrate cycles zero` dropped the playbook tables and their seeded
rows; forward migration recreated the schema. It did not preserve those rows.
`AXR1-009` (service cycles, pinned task graphs, private notes and exception
openers, append-only timeline, monthly generation, client workspace and the
restricted portal projection; `cycles.0002`) is recorded on final T8, using the
live S8.1 cycle capabilities and the shared portfolio loader. Gate repairs
preserve flag-off portal bytes against captured T8 parent/home templates,
return an existing authorized cycle after definition retirement or scope
changes without rewriting its pinned graph, and identify the client by name
and CNPJ alongside engagement, period and state on the write surface.
Tests: `tests/cycles/test_cycle*.py`,
`tests/clients/test_import_rollback_with_cycles.py`,
`tests/isolation/test_portal_cycles_isolation.py`,
`tests/portal/test_cycle_flag_off.py`, `tests/browser/test_cycle_workspace.py`
and `test_cycle_context.py`. Evidence: `.evidence/AXR1-009-fix-0.txt` through
`-fix-3.txt`, `-fixes-repeat.txt`, `-placement-baseline.txt`,
`-placement-migration.txt`, `-placement-gates.txt` and both-viewport screenshots.
The populated T9 schema round trip (`migrate cycles 0001`, then forward) dropped
and recreated the six new tables empty; six pre-existing audit events, one
source observation and their shared append-only protections survived. This is
schema reversibility, not restoration of the deleted cycle rows.
`AXR1-010` (the durable delivery outbox behind `cycles_r1`: `PhasedTenantTask`,
SKIP LOCKED claims committed before SMTP I/O and fence-checked finalization,
append-only attempt and effect receipts, the independent `effects-journal/` and
`reconcile_effects`, contact-only recipients, the pre-send hook registry, global
`DELIVERY_PAUSED` (default on) plus a firm pause gated on `automations.manage`,
the daily attempt cap, and the `/comunicacao/` queue; `delivery.0001`) is
recorded on final T9. An ambiguous SMTP outcome is `resultado_incerto`, never
retried, and opens a cycle exception; nothing is labelled delivered. Tests:
`tests/delivery/`, `tests/core/test_phased_tenant_task.py`,
`tests/security/test_outbox_log_hygiene.py` and
`tests/browser/test_delivery_queue.py`. Evidence: `.evidence/AXR1-010-red.txt`,
`-happy.txt`, `-failure.txt`, `-adversarial.txt`, `-migration.txt`,
`-gates.txt`, `-fix-1.txt`, `-fix-gates.txt` and both-viewport screenshots.
Operator gates OG-03 (a real send from the production host to an owner mailbox)
and OG-04 (retention and restore isolation of the journal prefix) remain
**open**; sending stays paused until both are recorded. Todos 11 to 29 and the
final verification wave remain open.

`AXR1-011` (the client portal's request inbox, upload against a request, the
company switcher and firm branding, all behind `cycles_r1`; `cycles.0003`,
`delivery.0002`, `tenants.0009`) is recorded on the `AXR1-010` outbox branch; like that row, it
describes code that is not yet on `main`. Client-visible `EvidenceRequest` and firm-only
`RequestInternal` are separate tables (S8.2): the portal role reads requests,
links and intakes under the RESTRICTIVE client policy and holds INSERT on
`cycles_evidencelink`, `cycles_documentintake` and `cycles_cycleevent` — the last
without SELECT, because the timeline also carries firm-side events. An upload
inserts rows only; "Enviado pelo cliente" and "Recebido" are derived from the
newest link, never stored, and neither takes the verified style. The same bytes
twice produce one `Document` and a second link, over 10 MiB is a pt-BR 400, and a
foreign request id is 404. The company switcher only selects among memberships
re-verified on every request, rotates the session key, and answers 403 with no
change for a non-member. Portal invitations leave through the outbox, with the
token minted at dispatch and the previous link answering 410; no raw token is
written to any row, attempt or log. Branding validates the colour against the two
product foregrounds — at least one must reach AA, and the portal renders the one
that does — and accepts a PNG of at most 200 KB, served through the app.
Carried forward, not closed: a queued portal invitation whose message the
provider rejects stays live, and the firm's "Cancelar convite pendente" control
is the recovery until a terminal-state withdrawal lands with T13/T16. Interim
until `AXR1-012`: a new upload is stored with an intake row in state `recebido`
and re-served only to its uploader, and `AXR1-012` replaces that rule with
quarantine and scanning. `OG-03` (a real production SMTP send) and `OG-05`
(malware scanning) are OPEN and neither is claimed by this work; sending stays
paused (`DELIVERY_PAUSED` defaults to true), so the only SMTP traffic in the
evidence is the tests' own fake peer. Tests:
`tests/portal/test_requests_inbox.py`, `test_upload_against_request.py` and
`test_recipient_without_seat.py`, `tests/cycles/test_duplicate_upload_relink.py`
and `test_request_services.py`, `tests/delivery/test_portal_invite_outbox.py` and
`test_reminder_reevaluation.py`, `tests/isolation/test_multi_company_switch.py`,
`tests/tenants/test_branding.py` and
`tests/browser/test_journey_portal_upload.py`. Evidence:
`.evidence/AXR1-011-baseline.txt`, `-red.txt`, `-happy.txt`, `-failure.txt`,
`-adversarial.txt`, `-migrations.txt`, `-gates.txt` and the five 390x844 portal
screenshots plus the 1440x900 staff one. The populated T11 schema round trip
reversed `delivery.0002`, `cycles.0003` and `tenants.0009` and re-applied them;
it dropped the four new tables with their seeded rows and left the pre-existing
outbox, document and append-only protections intact.

`AXR1-012` (quarantined document intake behind `cycles_r1`; `cycles.0004`) is
recorded on the `AXR1-011` branch. Every portal upload is written under
`quarantine/{tenant}/{client}/`, type-sniffed by magic bytes at offset 0 (PDF, XML,
PNG, JPEG only; declared type and extension ignored; polyglots, SVG, ZIP, HTML and
Office refused), scanned through the `Scanner` port (`ClamdScanner` over INSTREAM
with timeouts, `FakeScanner` for tests) in a phased task that streams outside any
transaction, and moved to its final key only after a clean verdict. Until then the
portal answers 404 and staff 409; malware is `rejeitado_malware`, never linked or
served, with the original retained. Retries back off to a 24 h deadline, then
`verificacao_indisponivel` opens one exception. XML is parsed only through
`defusedxml` and PDF text through `pypdf` (50 pages, 10 s checked between pages);
served files carry `Content-Security-Policy: sandbox`, `nosniff` and an RFC 5987
filename. This replaces the `AXR1-011` interim rule. `CLAMAV_ENABLED` defaults to
false, so until the gates below close, client uploads are downloadable by staff only
behind a warning and never re-served to the portal; the `clamav` sidecar is
digest-pinned behind a compose profile and was never started. Dependencies
`filetype`, `defusedxml` and `pypdf` are proposed. Tests:
`tests/cycles/test_intake_hardening.py` and
`tests/security/test_document_preview_headers.py`. Evidence:
`.evidence/AXR1-012-baseline.txt`, `-red.txt`, `-happy.txt`, `-failure.txt`
(`defusedxml` swapped for `xml.etree` fails the XXE cases), `-gates.txt`,
`-adversarial.txt`, `-mutations.txt` and `-migrations.txt`. OG-05 (host memory
and freshclam on Lightsail), OD-11 (host upsize) and OG-04 (quarantine prefix
lifecycle) remain **open**. Carried to `AXR1-013`: the legacy portal vault upload
still bypasses this pipeline, and the Celery soft limit that bounds one slow PDF
page. Todos 13 to 29 and the final verification wave remain open.

`AXR1-013` (deterministic document extraction, field provenance, wrong-company
quarantine and staff review; `cycles.0005`, `obligations.0019`) is recorded on
the final `AXR1-012` branch, not on `main`. The scoped `/documentos/` queue and
review route expose XML/PDF suggestions and their origin without modifying
original bytes. Unknown XML layouts and images make no automatic verification
claim. Accepted corrections stay in `Review`; rejected files retain a pt-BR
reason visible to their client. Capability, portfolio scope and rendered
version are checked under the actual cycle/request locks; stale writes answer
409 without effects. Wrong-company findings suspend links, preserve ownership
and propagate their exception to later cycles when the same bytes are linked
again. The legacy vault upload path now enters intake, and historical
unverified client uploads fail closed when served. The PDF worker's soft/hard
bounds are 15/30 seconds; interruption handling does not claim a timed
live-worker kill.

The review table contains its hidden labels at 390 pixels while retaining
accessible associations and table-only horizontal scrolling. Keyboard
decisions wait on navigation-load signals, not timing sleeps. An independent
review confirmed `8f5f475`, closed both review findings, reproduced six
XML/PDF viewport cases, and proved that removing the containment class fails
the mobile journey at 628 versus 390 pixels. Both source hashes were restored
and both keyboard journeys passed again. Native headless PDF rendering is
unverified; actual clicks returned the unchanged original bytes and safe
headers. The populated schema reversal preserved pre-existing documents,
links and shared protections; it dropped the added review/extraction rows,
which forward migration does not restore.

Tests: `tests/cycles/test_extraction.py`, `test_wrong_company_upload.py`,
`test_review_concurrency.py`, `test_duplicate_upload_relink.py` and
`tests/browser/test_document_review.py`. Evidence:
`.evidence/AXR1-013-reviewer-final-attacks.txt`, `-reviewer-remaining.txt`,
`-ui-fix-red.txt`, `-ui-fix-render.txt`, `-ui-fix-gates.txt`,
`-rv-8f5f475-baseline.txt`, `-rv-8f5f475-mutation.txt`,
`-rv-8f5f475-restored.txt` and `-rv-8f5f475-cleanup-final.txt`.
All owned review containers, volumes, processes and temporary paths were
removed. Existing semantic-template diagnostics and a favicon 404 are
documented, not suppressed. OG-03/OG-04/OG-05 and OD-11 remain **open**;
no production email, scanner activation or pilot authorization is claimed.
Search and AI-shadow exclusion proofs remain in `AXR1-021`/`AXR1-024`.

`AXR1-014` (owned exceptions, deterministic openers, policy-restricted risk
acceptance and client-safe help; `cycles.0006` and `cycles.0007`) is recorded on
the final `AXR1-013` branch. Staff can assign, triage and resolve from the cycle
or client workspace. Scoped evidence links reach the actual document, message
or current subject; reload, Back and close preserve the selected drawer and
natural keyboard focus. Internal instructions and references live in the
portal-denied `ExceptionInternal` companion, not the portal-readable row.
Populated reversal/reapply preserved stored private values, aggregate identities,
T13 data, grants and shared protections.

The rejected portal-invitation finalizer withdraws the invitation and retains
its rejected delivery and audit record. Its exception belongs to the newest
applicable open cycle; with none, no cycle or unrelated exception is fabricated.
Replay does not resend or duplicate the terminal records. A further independent
portfolio-revocation probe found a stale authorization lookup; the locked cycle
loader now rechecks visibility before writes, with a real committed
second-connection regression.

The independent gate confirmed code `c956af9` after closing privacy, navigation
and revocation findings. Tests include `tests/cycles/test_exceptions.py`,
`test_exception_privacy.py`, `test_exception_policy_boundaries.py`,
`test_exception_evidence.py`, `test_exception_navigation.py`,
`test_exception_subjects.py` and `tests/browser/test_exceptions.py`. Evidence:
`.evidence/AXR1-014-rv-final.txt`, `-rv-adversarial.txt`, `-rv-mutants.txt`,
`-rv-restored.txt`, `-rv-revoke-red-final.txt`, `-rv-revoke-green.txt`,
`-rv-revoke-typing.txt` and the final natural/terminal browser captures.
Thirty-six natural evidence journeys covered nine reason families on both staff
surfaces at both widths; actual portal SQL and rendered privacy controls passed.
All owned resources were removed. Existing portal placeholder copy and four
JavaScript diagnostics remain documented; live operator and owner gates remain
**open**. No production delivery or pilot authorization is claimed.

`AXR1-015` (immutable exact-effect proposals and append-only approval decisions;
`cycles.0008`, `authz.0007`, `tenants.0010`) is recorded on the final
`AXR1-014` branch. The closed manifest uses canonical UTF-8 bytes and a SHA-256
digest to bind selected recipients, documents, evidence, source facts, money,
policy and authority. Propose, approve, reject, revalidate and cancel are scoped,
versioned services. Material changes invalidate the old revision without
rewriting its bytes or decisions; unrelated facts follow the action-specific
invalidate/warn/ignore matrix.

Email proposals freeze real CRLF MIME bytes, headers and their hash, using
persisted proposal facts rather than the render clock. Relevant portfolio
assignment identities bind both initiator and approver authority: removing
and re-adding an assignment cannot revive a decision. Firm-wide and unrelated
assignments do not cause blanket invalidation. Self-approval is refused by
default; the sole-staff exception requires owner policy, audited acknowledgment
and explicit acknowledgment for each decision.

The staff card shows the frozen effects before decisions, including
keyboard-accessible mail headers. Approval records only a decision: it does
not send mail, publish files or prove execution. Executor integration remains
`AXR1-016`. The independent gate confirmed `6089575` after closing both MIME
and portfolio-history findings. Populated reversal/reapply preserved inherited
T14 private values, T13 data and shared protections; it intentionally removes
the new proposal/approval rows.

Tests include `tests/cycles/test_canonical_digest_vectors.py`,
`test_effect_manifest.py`, `test_approval_invalidation.py`,
`test_effect_sources.py`, `test_revalidate_revocation.py`,
`test_approval_storage.py`, `test_approval_authority_races.py` and
`tests/browser/test_approval_card.py`. Evidence:
`.evidence/AXR1-015-rv-final-std.txt`, `-rv-final-probes.txt`,
`-rv-final-restoration-cleanup.txt` and six fresh desktop/mobile card captures.
Ten independent semantic cases passed; the updated recipient mutation caused
three behavioral failures, followed by three restored passes. All owned
resources were removed. Nonblocking reason-formatting, UUID recipient labels
and inherited default error pages remain documented; owner/operator gates
remain **open**. No production effect or pilot authorization is claimed.

`AXR1-016` (approved client publications, email notices and acknowledgements;
`cycles.0009`) is recorded on the final `AXR1-015` branch. The four
consequential executors (publication, email notice, cycle completion and
request waiver) rebuild live facts and rerun exact-effect revalidation before
their first write, and authorization holds through the durable dispatch-start
write; SMTP I/O happens outside the transaction through the existing outbox
gateway, one envelope recipient per message, and an unknown outcome is never
resent. `ClientPublication` is portal-readable; `ClientAcknowledgement` is
portal insert-only and append-only and binds the exact publication digest. An
authenticated portal GET inserts a viewed event (HEAD, prefetch and anonymous
requests do not), and the explicit receipt is independent of the SMTP outcome.
The firm queue shows provider outcome and receipt separately and never claims
"entregue" or "lida". Withdrawal needs a new approved proposal; the waiver and
completion forms now propose instead of bypassing approval. Ordinary completion
still requires the inherited evidence-index guard owned by `AXR1-018`.

The independent gate confirmed `75272c6`: both revocation/dispatch orderings
against a real SMTP peer, a biting revalidation-removal mutation, populated
reverse/forward preserving 20 inherited tables, and inspected desktop/mobile
keyboard journeys. Tests include `tests/cycles/test_publication_flow.py`,
`test_approval_revocation.py`, `test_dispatch_race.py`,
`test_ack_independent_of_smtp.py`, `tests/isolation/test_publication_scope.py`
and `tests/browser/test_publication_ack.py`. Evidence:
`.evidence/AXR1-016-gates.txt`, `-failure.txt`, `-adversarial.txt` and the
reviewer's `-rv-*` artifacts. OG-03, OG-04 and every owner/operator gate remain
**open**; only a local fake SMTP peer was used.

`AXR1-017` (source observations and the honest coverage panel; `cycles.0010`,
`obligations.0020`) is recorded on the final `AXR1-016` branch. The obligation
calendar writes `calendar_scheduled` assertions per client month, staff record,
correct and retract "guia emitida" and "pagamento informado" facts against a
locked subject chain, and a `comprovante` request binds its obligation only
while it is a draft and only inside the engagement's half-open date window. A
submitted receipt stays `pending_review` until a clean review accepts it, and it
is then labelled manual. `coverage_for(client, period)` answers each in-scope
obligation with `covered_by`, `last_success` and one of unknown, not_applicable,
manual, pending_review, stale or refresh_failed. The panels in the client
workspace and the cycle render these states verbatim, and nothing manual or
unknown renders green or "confirmado". `coverage_for_clients()` answers the same
question for an already scoped set of clients with four bound queries, and
`AXR1-019` uses it. R1 produces no official source.

The first independent review rejected the original head with seven blocker
groups. The repair (`71fd6b8`) closed them, and a second independent review
reproduced every original attack as passing. It found one more gap (receipt
requests ignored engagement dates), which `1902cf7` closes. A third independent
review confirmed that fix: eight real HTTP scenarios, a biting mutation and its
restoration, historical reads, and the complete serial gates at `1902cf7`.
Tests include `tests/obligations/test_coverage.py`, `test_coverage_repair.py`,
`test_bulk_coverage.py`, `test_receipt_dispatch.py`,
`tests/cycles/test_payment_evidence_labels.py`, `test_receipt_repair.py`,
`test_observation_locks.py`, the extended `tests/ui/test_status_render_policy.py`
and `tests/browser/test_coverage_panels.py`. Evidence:
`.evidence/AXR1-017-repair-gates.txt`, `-repair-failure.txt`, `-n1-red.txt`,
`-n1-full-gates.txt` and the reviewers' `t17-rv2` and `t17-rv3` artifacts. Every
owner/operator gate remains **open**.

`AXR1-018` (justified completion, amendments, evidence index, bundle export and
retention classes; `cycles.0011`-`0015`, `delivery.0003`, `obligations.0021`) is
recorded on the final `AXR1-017` branch. Completion has one authority: the
cycle page shows a linked checklist of request, exact-effect and evidence-index
guards, and "encerrado com pendências" needs a reason for each item that is
unmet at that moment. The reason is keyed by guard, subject and revision, and
the set is recomputed under the cycle lock. Amendments never rewrite the frozen
completion generation, and reopening needs `cycles.reopen` and a separate
approver. The evidence index validates against
`apps/cycles/schemas/evidence_index_v1.json`. The bundle export is a phased
tenant task: a committed `building` claim, storage I/O outside any transaction
with byte caps on actual bytes, and a fenced finalize. Download goes through
the app only, for the firm only, and the bundle is never labelled a
certificate, an official audit or tamper-proof. Retention classes, legal holds
and `purge_expired_content` (dry-run by default, counts only) cover documents,
links, messages and AI-derived content, and `docs/retention.md` lists the
streams.

The independent gate review confirmed `cae2ba6` (tree `5c16466`) with zero
blockers. All eleven attacks passed, as did real-role probes of the narrow
SECURITY DEFINER retention trigger: fixed search_path, revoked EXECUTE, RLS and
hostile-shadow attempts. Seven populated migration round trips preserved rows
and grants. The guard-test audit covered 29 registrations and six existing-test
edits. Removing the open-exception guard made the completion tests fail, and
restoring it passed them. A real 101,355-byte bundle download matched its hash
and manifest. Evidence: `.evidence/AXR1-018-happy.txt`, `-failure.txt`,
`-gates-placed.txt` and the reviewer's `-rv-*` artifacts. OG-04, OG-08 and every
owner/operator gate remain **open**; purge stays dry-run.

`AXR1-019` ("Hoje": morning briefing and explainable exception queue;
`cycles.0016`) is recorded on the final `AXR1-018` branch. For tenants with
`briefing_r1`, providers for obligations, overdue requests, exceptions,
approvals waiting, delivery outcomes, divergent documents and stale coverage
each yield a `BriefingItem`, ranked by `BRIEFING_RULES_V1` factors with a stable
tie-break and keyset pagination. Items and counts come only from
`visible_clients`. Saved views are re-authorized at render. A "Não relevante"
dismissal needs a reason, applies only to the acting user, and expires when the
underlying fact changes. The page shows "Calculado às HH:MM", "Mudanças desde
ontem", loading and stale-view states, and a 403 drops stale counts. Tenants
without the flag keep the old dashboard byte-identical. With 1,000 clients, the
whole Hoje request takes exactly **25** statements, under the plan's
`django_assert_max_num_queries(25)`. The repaired base started at 8,032. No
authorization answer is cached or shared. Eight existing exact query-count
registrations went down because their counts really fell; none went up.

The independent gate review found two blockers at `0fc87e3`: one stale
coverage item raised the 1,000-client Hoje request to 26 statements, and a
maximum-length client name widened the page sideways. Repair `3c8e5e2` reuses
the engagement batch the coverage reader already loads and wraps the client
line; a scale test in which every provider has items, stale and
refresh-failed coverage included, failed at 26 and now stays within 25, and a
long-name browser test failed and now passes at both viewports. No query-count
registration changed. An independent confirmation review of the unchanged
`3c8e5e2` returned **confirmed** with zero blockers: richer 1,000-client
requests each used exactly 25 statements, hostile and maximum-length names fit
both viewports, and removing the fix makes the new scale test reject 26
statements. Evidence:
`.evidence/AXR1-019-happy.txt`, `-failure.txt`, `-gates-placed.txt`,
`-adversarial.txt`, `-fix-red.txt`, `-fix-red-overflow.txt`, `-fix-green.txt`,
`-fix-green-overflow.txt`, `-fix-gates.txt` and the reviewers' `T19-RV-*` and
`T19-RV2-*` artifacts. Every owner/operator gate remains **open**.

`AXR1-020` (bulk operations with an eligibility preview, per-item results,
cancel and resume; `cycles.0017`) is recorded on the final `AXR1-019` branch.
For tenants with `bulk_r1`, a `BulkOperation` freezes its preview as a
snapshot with a digest and an expiry, and execution uses only that snapshot.
An expired or changed preview is refused, and confirming requires typing the
exact item count. Each `BulkItem` is unique per tenant, client, operation and
subject. That key is also the runner's claim and idempotency key. The runner
processes each item in its own tenant transaction, with a fenced claim and
per-item capability and version checks. A changed record is skipped as
changed, cancel stops before the next claim, and resume after a crash creates
no duplicate effect. Bulk approval delegates every item to the `AXR1-015`
per-item digest and revalidation. The results page polls and stops at a
terminal state. It says "Concluído com falhas: N" whenever an item failed, and
its CSV export is formula-safe. Published playbook versions are labelled with
the playbook name and version number, and the preview shows how much of the
daily quota remains.

The independent gate review found one blocker at `da8da54`: notice previews
did not show the remaining daily quota the plan requires (with a cap of 100
and 97 sends reserved, the preview omitted the 3 left). Repair `1bec524` read
the same `remaining_quota()` helper the Comunicação queue uses, but stored the
value when the preview was created; its confirmation review found that
reloading a preview after reservations rose to 100 still showed 3 while the
queue showed 0. Repair `549d202` reads the remaining quota live on every
render of the preview (one read per render, none per item and none on the
progress poll), says when the previewed notices exceed it and that the excess
waits in the queue until reservations renew, and removes the stored value.
Previewing reserves nothing; execution still uses the frozen item snapshot
and the send-time quota. Tests failed before each repair and pass after it,
and rendering a stale or capped value makes them fail again.
An independent confirmation of repair 2 returned **confirmed**
with zero blockers: the same preview showed 3 at 97/100, stayed 3 after
another firm's reservations and reloaded to 0 after its own; a confirmed run
used the frozen digest, version and audience, queued one notice and made no
send attempt; and rendering the stale value makes the HTTP test fail. Evidence:
`.evidence/AXR1-020-happy-placed.txt`, `-failure.txt`, `-rv-quota.md`,
`-fix-red.txt`, `-fix-green.txt`, `-fix-mutation.txt`, `-fix2-red.txt`,
`-fix2-green.txt`, `-fix2-mutation.txt`, `-fix2-gates.txt` and the reviewers'
`T20-RV*` artifacts. Every owner/operator gate remains
**open**.

`AXR1-021` (permission-scoped search and command palette; `core.0001`) is
recorded on the final `AXR1-020` branch. For tenants with `cycles_r1` (the R1 product shell; search answers 404
otherwise),
`search(actor, q, kinds)` reads only querysets scoped by `visible_clients`,
needs at least two characters and returns at most five results per kind
without revealing counts. Documents are found by filename only, never by their
contents. Standalone and vault documents are included and open the page the
actor is allowed to use, and a document whose extracted CNPJ diverges never
surfaces. Matching is accent-insensitive through `unaccent` and `pg_trgm`, with
trigram GIN indexes on client names, the normalized CNPJ and request labels;
the migration asserts that both extensions exist and does not create them.
Full-width wildcards match only themselves. A header search field and a
`Ctrl/Cmd+K` palette render server results as a keyboard-navigable listbox,
no tenant value is interpolated into `x-*` or `hx-*` attributes, and the
portal has no global search in R1. Long filenames, client names and labels
wrap at phone width. Tailwind no longer scans the frozen legacy-dashboard test
fixture; the only stylesheet change from that is the removal of the unused
small-breakpoint inline-display rule.

The pre-placement adversarial review found standalone documents unsearchable
and full-width wildcards unescaped; both were fixed with regression tests that
failed first. The independent gate review at `5235bea` returned **confirmed** with zero
blockers after attacking scope, runtime, the migration, mutations and keyboard
and browser behaviour; its own serial gates passed (3916 default, isolation
288, CNPJ 29, browser 78). It also noted that a permitted long, unbroken filename did not reflow
at 390 px (palette list 2507 px wide inside 318 px; `/busca/` 2540 px). Repair
`51afd08` makes names wrap in both renderings, and a 390/320 px browser test
failed before it and passes after. The confirmation review of the unchanged
`51afd08` returned **confirmed** with zero blockers: long names wrapped at every
tested width, with full accessible names and keyboard selection intact, and the
pre-repair layout failed its four new tests. Evidence: `.evidence/AXR1-021-failure.txt`,
`-static-drift.txt`, `-gates-place2.txt`, `-fix-red.txt`, `-fix-green.txt`,
`-fix-visual.md`, `-fix-gates.txt` and the reviewers' `t21-rv-*` and
`T21-RV2-*` artifacts. Every owner/operator gate remains **open**.

`AXR1-022` (firm onboarding, settings, entitlements, usage, manual billing path,
export and offboarding; `authz.0008`, `tenants.0011`) is recorded on the final
`AXR1-021` branch. The admin "Nova firma" flow creates a tenant, an owner
invitation through the outbox and the default flags, and writes a
`PlatformEvent`. Behind `firm_settings_r1`, `/configuracoes/` covers firm profile
and branding, team and capability grants, entitlements, per-user daily summary
e-mail, retention classes and legal holds, account export and cancellation. A
tenant quota fence serializes every client writer before the client lock;
exceeding the active-client limit is a soft block, and the import preview shows
the limit. The account export is an asynchronous, owner-only ZIP of CSV metadata
with a documents manifest and a SHA-256 manifest, built by the `AXR1-018`
`PhasedTenantTask` machinery. Cancellation makes the firm read-only after the end
date, keeps export open for 90 days and then runs the per-class purge, which stays
a dry run until OG-08. The manual `BillingAccount` is operator-maintained:
`past_due` shows the owner a banner and `suspended` makes the firm read-only with
export still allowed; no card data is stored. Append-only `UsageRecord`s, written
nightly, feed an owner-only monthly cost estimate that is labelled as an estimate
and marked incomplete while any unit price is unknown. Ending an engagement
deactivates the client's portal memberships after a 30-day grace period. The new
owner-only capability `tenants.account` guards export, cancellation and the
estimate.

The independent gate review at the placed head `6a78683` attacked placement fidelity, the
guard registrations, the active-client limit under a race, cancellation and suspension, owner-only
export, usage and billing, offboarding and the header. Its own serial gates passed (4072, isolation
306, CNPJ 29, browser 90). It returned **needs-fix** with one blocker: with `firm_settings_r1` on, at 1280 px with a classic scrollbar,
an ordinary 22-character firm name left only 2-6 px of spare room in the header row.
Repair `b05d7af` lets the firm name shrink and truncate between xl and 2xl, keeping
the full name accessible and 16 px reserved before search. Repair `af8e053` holds the
row at 1536 and 1920 px as well, where the text labels and e-mail return. The flag-off
header stays byte-identical to `AXR1-021`. The confirmation review of both commits
returned **confirmed** with zero blockers: the
header held one row with at least 16 px spare in 90 measured cases from 1280 to 1920 px and at each
breakpoint boundary, the flag-off bytes matched `AXR1-021`, removing the rule failed the test,
and its own reruns passed (22 header, 471 UI and 77 briefing tests, static equality). The first full run's five failures
were guard registrations for T22's additions (capability count, a no-edit trigger,
a composite-key anchor, four platform-query justifications, fourteen labels); no
guard was weakened. Evidence: `.evidence/AXR1-022-happy.txt`, `-failure.txt`,
`-failure-restored.txt`, `-fix-redgreen.txt`, `-gates-final.txt`,
`-placement.md`, `-gates-placed.txt` and the reviewer's `T22-RV*` artifacts.
OD-04 and OG-08 remain **open**, as does every other owner/operator gate.

`AXR1-024` (AI evaluation harness and shadow document-classification runner;
`automation.0001`) is recorded on the final `AXR1-022` branch. It is an offline synthetic
harness and a private, closed-by-default shadow runner, not an AI product feature: no view
or admin shows `AgentRun` or `EvalRun`, and deterministic extraction and review are
unchanged. A real call needs `AI_PROVIDER`, a lifted global disable, real-data approval and
the tenant's `ai_shadow` flag.
- **Clients and transport.** Clients split `prepare()` (all local checks, no I/O) from
  `send()` (one bounded HTTPS request with no tools, redirects or retry).
- **Harness.** `build_eval_set` regenerates 300 synthetic documents and an injection corpus
  byte-for-byte. `run_eval` reports per-field precision and recall, wrong-client attribution
  with a Clopper-Pearson 95% upper bound, abstention, injection success, latency and cost.
- **Shadow runner.** It takes only clean, linked documents and enforces advisory-locked
  caps: per source and client, per tenant and day (100 calls and R$5), and R$50 globally per
  month. It also has a kill switch, and it reserves R$0.025 per attempt before any I/O.
- **Output validation.** Rejects other clients' identifiers and web references in any
  script.
- **Usage metering.** One confirmed `UsageRecord(ai_shadow_calls)` per run that reaches the
  provider, committed between `prepare()` and `send()`, so local refusals count nothing.

The independent gate review of the placed head `17ccfe8` returned **needs-fix** on two
blockers:
- runs refused before dispatch were metered;
- Unicode web addresses passed the ASCII-only output check.

Everything else it attacked held. Repair `5b2c242` fixes both, each with a red test, a green
run and a mutation. The confirmation review of `5b2c242` returned **confirmed** with zero
blockers: the refusal paths record no usage, a dispatched call counts once, Unicode web references are
refused before storage, both mutations fail, and its targeted reruns passed (432). Evidence:
`.evidence/AXR1-024-happy.txt`, `-failure.txt`, `-placement.md`, `-usage-*.txt`, `-fix-*.txt`,
`-gates-placed-17ccfe8.txt`, `-gates-final.txt` and the reviewers' `T24-RV*` artifacts.
OD-05 and every other owner/operator gate remain **open**, and `ai_shadow` is off everywhere.

`AXR1-027` (operational signals and the restore path for the delivery outbox; `delivery.0004`,
`delivery.0005`) is recorded on the final `AXR1-024` branch.
- **Readiness.** `/readyz` returns 503 with a `reason` key when the oldest queued message or the
  dead-letter count passes its env-configured threshold. `/healthz` and its monitored keywords are
  byte-identical.
- **Automações page.** A capability-gated Automações page under Comunicação shows registered jobs
  with their last and next runs. Every shipped job has a pt-BR label. The page lets the firm pause
  and resume delivery, and retry or cancel dead letters: a retry creates one successor and cancels
  the original, and a stale submission returns 409.
- **Restore and runbooks.** `ops/RESTORE.md` adds the Axorim restore: pause delivery, restore,
  `reconcile_documents`, `reconcile_effects`, review `resultado_incerto`, unpause.
  `ops/PILOT-RUNBOOK.md` maps the new alerts to actions, and `docs/lgpd.md` documents the outbox kill
  switches.
- **Log hygiene.** The log scrubber's canaries cover outbox payloads.

Placement onto T24 needed one label fix (`c1451a2`). The header is no worse than its parent in
any of 312 measured cells. The independent gate review at `c1451a2` returned **confirmed** with zero
blockers: readiness thresholds (503 with reason keys), byte-identical `/healthz`, the Automações controls at
both viewports with tenant denials, restore reconciliation without replay, a biting mutation, and its own
serial gates (4253, isolation 309, CNPJ 29, browser 94).
Evidence: `.evidence/AXR1-027-*` (the worker's original gates and failure control, plus placement,
geometry, labels red/green, adversarial probes, placed gates) and the reviewer's `T27-RV*`
artifacts. OG-06, the operator restore drill on an isolated host, and every other
owner/operator gate remain **open**.

`AXR1-028` (synthetic scale data, query budgets and read-latency measurement) is recorded on the final
`AXR1-027` branch.
- **Seed.** `seed_axorim_demo` builds three deterministic firms of 100, 1,000 and 1,000 clients with
  messy data, and it refuses to run against production.
- **Budget tests.** Query-budget tests cover the named reads, with a database-wide `ANALYZE` after seeding
  so planner statistics reflect the seeded volume. There are no wall-clock limits in tests.
- **Measurement.** `ops/perf/measure_reads.py` reports constrained-local p50/p95 per named read, and
  `ops/perf/followups.json` lists the reads over target: Hoje p95 574 ms against 500 (676 ms at the placed head), and the client
  workspace (46) and cycle detail (50) against the 25-query target. No SLO is claimed.

A suite-order stall of the perf test is recorded with a candidate cause (mixed planner statistics),
reproduced only as a 12x slowdown, not proven. The independent review at `6bdbd41` returned **confirmed** with zero
blockers; every owner/operator gate remains **open**.

`AXR1-023` (time- and purpose-limited support grants; `tenants.0012` and `0013`) is recorded on the final `AXR1-028`
branch.
- **Grant model.** An owner approves a `SupportGrant` at `/configuracoes/suporte/`. The grant names its
  purpose and scope, can be limited to clients, and lasts at most 72 hours, enforced in the database. It can
  be revoked immediately and is never extended silently.
- **Support mode.** Platform staff with an active grant enter through the middleware with a visible "Acesso
  de suporte ativo até …" banner. Document bytes and message bodies are redacted, and approving, sending or
  downloading is refused.
- **Audit and admin.** Every support request is audited for the owner. The superuser admin needs an active
  grant for tenant data.

Placement added a root-required commit:
- the "no support console" guard now also sees `suporte`, with exactly the three owner routes allowed;
- the app role can no longer delete support grants (no code path needs it; revoke-then-grant in the migration);
- unscoped reads are pinned to the grant;
- support mode covers the T24 and T27 surfaces;
- the inactive banner renders no bytes, so the legacy dashboard stays byte-identical.

The first independent review found three blockers: client data under a metadata-only grant, unaudited page
views and writes accepted after revocation. Two more came from T25 and T29: the approval form's
unnamed and colliding client checkboxes, and the owner having no view of the request trail.
The next review found six additional gaps: sibling audit-admin scope, the admin banner,
write/revocation ordering, silent window extension, unreachable historical trail pages,
and credentials in audited URL paths. Six focused commits repaired them with failing-first
tests. CF2 independently **confirmed** `b02cf0e` with zero blockers: 625 targeted tests,
312 isolation tests, 29 CNPJ tests, 102 browser tests, 16 additional cases and a populated
migration round-trip. The delivered code at `d477b7e` is identical apart from this status
document. Both author and reviewer lanes were torn down with receipts.
Evidence: `.evidence/T23-CF2-review.md`, `T23-CF2-cleanup.txt` and `AXR1-023-fix3-teardown.txt`.
Every owner/operator gate remains **open**.

`AXR1-025` (golden browser journeys and accessibility evidence; `tenants.0013_backfill_billing_accounts`,
ordered after `AXR1-023`'s `0013_support_grant_window_immutable`) is recorded on the final `AXR1-023` branch.
- **Journeys.** Journey A and failure journeys B, D/Q, I, J and K run in a real browser at both viewports,
  with a slow-network upload retry and no fixed sleeps.
- **Accessibility evidence.** axe covers every S5.5 screen, Automações and Suporte included; focus lands on
  the updated heading after swaps; and keyboard paths are tested. `ops/AXORIM-A11Y-CHECKLIST.md` holds the
  manual screen-reader scripts, with an operator table that is still open.
- **Fixes.** The journeys forced five product fixes: a calendar-label 500, focus after swaps, pt-BR refusals,
  the billing-account backfill for older firms, and the approval card's recipient name and state badges.

No WCAG conformance is claimed. The independent review **confirmed** `b4c9a66` with zero blockers: it reran the full suite (4336),
isolation (312), CNPJ (29) and the browser suite (134, the 32 new cases three times), checked the populated
migrations and reproduced the Journey I failure proof. Evidence: `.evidence/T25-RV4-review.md`. Every owner/operator gate remains
**open**.

`AXR1-026` (cross-tenant and cross-client isolation sweep; tests and engineering documentation only)
is recorded on the final `AXR1-025` branch. The explicit 45-model registry covers real RLS reads,
portal decisions, vacuity controls and catalog-derived composite foreign keys. Route checks include
the support trail and company selection, with no foreign markers or counts in denial bodies.
The suite also covers tenant-aware tasks, real same-CNPJ imports followed by search/export,
assigned-client permissions and cross-firm support grants.

Independent review repaired five checks that did not prove their intended protection, plus a
company-selection denial-body omission found during confirmation. The final review **confirmed**
`80fe953` with no blockers: exact-head isolation passed **628** tests with **28** schema-justified
skips (15 client-parent, 13 tenant-parent), and nine mutation invocations produced **16** expected
assertion failures. All restored checks passed. Synthetic FK probes verify role, trigger, policy
and row restoration after both successful and refused INSERTs; production protections are unchanged.
Evidence: `.evidence/T26-RV5-review.md`, `T26-RV5-live.txt` and `T26-RV5-cleanup.txt`.
Accepted limits remain in `docs/residual-risks.md`; every owner/operator gate remains **open**.

`AXR1-029` (launch-readiness engineering) is recorded on the final `AXR1-026` branch.
Nine pt-BR help articles and their index explain shipped behavior; apex-only product and pricing
pages retain the app-mei name and "Fale conosco". Exact approval is distinguished from authorization
of templated messages by a published playbook or tenant policy, including cycle-independent invitations.
The document guidance distinguishes extractable PDF/XML identifiers from images requiring manual review.
The public/help footer meets the measured contrast threshold without changing the frozen legacy output.

Independent review **confirmed** `46e1eed` with no blockers. Real SMTP and image probes agreed with
the copy; both legacy dashboard byte comparisons passed; all **44** public/help renders fitted at
1440 and 360 pixels with no serious/critical axe violations and measured footer contrast **7.1043:1**.
A fresh forbidden-claim mutation failed as intended and was restored.
Evidence: `.evidence/T29-RV-review.md`, `T29-RV-final-runtime.txt`,
`T29-RV-final-pages2.txt`, `AXR1-029-RV-final-happy.txt` and `AXR1-029-RV-final-failure.txt`.
The five legal/partner drafts remain private MINUTA documents for professional review.
Every owner/operator/contact/cohort gate in `ops/AXORIM-R1-GO-NO-GO.md` remains **open**.
This is engineering approval, not launch authorization or a WCAG-conformance claim.

`AXR1-031` (customer-facing destination labels) is recorded on the final `AXR1-029` branch.
Individual and bulk cards show meaningful destinations for publication, email, completion and waiver,
and the quota explanation uses neutral wording. Stored manifests, internal provider identifiers,
canonical bytes, digests and execution code are unchanged.
Independent review **confirmed** `bc2c0cb` with no blockers, resolving F1's naming finding B1.
Its own 108 render/manifest checks and 12 browser cases passed, including individual reopening.
The reviewer inspected 20 author captures and 24 independent captures at desktop/mobile widths
and verified its resource teardown. Evidence: `.evidence/AXR1-031-review.md`,
`AXR1-031-review-verdict.json`, `AXR1-031-F1-delta.md`, `AXR1-031-happy.txt`
and `AXR1-031-failure.txt`. Test-oracle limitations and the inherited generic bulk reopening
label remain documented in that review. No owner/operator gate is closed by this repair.

`AXR1-032` (support decision authority) is recorded on delivered `AXR1-031`.
Both approval and revocation lock a fresh actor before rechecking activity, the owner
seat and the account capability, then lock the grant. Authority removal that commits
first prevents the decision and its audits; a decision holding the fence commits first.
Existing scope, immutable windows, grant locking and both audit streams are preserved.
Independent review **confirmed** `f4e8c87` with no blockers, passing 14 independent
checks and closing F2-B1. The worker's failing-first result was eight failures and two
passes, followed by 48 targeted passes. Reviewer and author resources are removed.
Evidence: `.evidence/AXR1-032-review.md`, `AXR1-032-review-verdict.json`,
`AXR1-032-F2-delta.md`, `AXR1-032-review-live.txt`, `AXR1-032-failure.txt`
and `AXR1-032-integrated-gates-summary.txt`.
Final F2 code-quality verification is **APPROVE** at that implementation head;
`.evidence/AXR1-F2.txt` records its scope and limits. Whole F1, F3/F4 and all
OG/OD gates remain open. No launch, merge or deployment is authorized here.

Execution of the full plan resumed on 2026-10-05 at the owner's instruction.
The final verification wave remains open.

`AXR1-033` (accepted invitation contact binding) is recorded on delivered `AXR1-032`.
Contact-issued invitations retain an explicit nullable origin through token refresh.
Acceptance atomically links only that eligible same-client contact to the accepted
membership; address-only and historical invitations do not guess a contact by email.
Mailbox, consent, token, expiry and existing-association checks remain enforced.
The original firm-roster contract and unchanged publication recipient guard are preserved.
Independent review **APPROVE**, high confidence, at `c6653b0`: 71 independent
checks passed, losing the contact link broke both publication journeys as expected,
restoration passed, and the populated migration reversed/reapplied as `app_migrator`.
Root's actual desktop/mobile continuation accepted a fresh invitation for the
retained Aurora contact and created its publication approval card; consumed-token
replay returned 410. No fixture assigned the contact's membership.
Evidence: `.evidence/AXR1-033-review.md`, `AXR1-033-review-cleanup.txt`,
`AXR1-033-root-qa.md`, `AXR1-033-gates-summary.txt`, `AXR1-033-happy.txt`
and `AXR1-033-failure.txt`. Author and reviewer runtime lanes are removed;
separate retained F3 data remains for the unfinished final journey.
This approves T33, not publication execution/acknowledgement/closure, wider
F3/F4 fidelity, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-053` (versioned tenant notice authorization) is recorded on delivered `AXR1-033`.
A tenant-scoped, monotonically versioned notice policy holds exact manual kind grants
that only a fresh ordinary owner can save or revoke, under lock, expected version,
capability check and an append-only event carrying the acknowledgement snapshot.
Templated intents bind their authorization basis immutably at enqueue: either the
cycle's published playbook or the acknowledged policy revision. Dispatch revalidates
policy revision, owner seat, capability and recipient before any SMTP; revocation or
re-acknowledgement cancels obsolete intents with zero sends. No migration consent,
historical acknowledgement or consequential-effect allowlisting. The policy table is
RLS-forced, portal NONE, and its migration grants `app_runtime` explicitly.
The owner surface was exercised for real (password and MFA) in cycles and import-only
shells at desktop and mobile. Independent review **APPROVE**, high confidence, at
`136a267`: focused, adversarial, Chromium/axe and fake-SMTP checks passed; removing
the pre-send authority check made the policy-revision test transmit and fail, and
restoring it passed. The reviewer relied on source equivalence for the full default,
isolation and CNPJ suites, which ran at `f0a2d9e` (only a browser fixture differs).
Evidence: `.evidence/AXR1-053-happy.txt`, `AXR1-053-failure.txt`, `AXR1-053-review.md`,
`AXR1-053-review-cleanup.txt`, `AXR1-053-root-ui.md`, `AXR1-053-final-gates-r3.txt`
and `AXR1-053-final-browser-r1.txt`. This approves T53, not T48 import-origin routing,
T39 automatic reminders, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-048` (import-only invitation routing) is recorded on delivered `AXR1-053`. The
import-gated contact door now issues a durable `contact_portal_invite` intent under the
owner's exact, separately acknowledged notice-policy grant, bound with feature
`import_r1`, instead of refusing when cycles is off; a `portal_invite` grant does not
authorize it. Claim, egress and worker selection admit each kind only under its own
originating flag and global kill. Migration `delivery.0007` widens only the T53 binding
guard for the new kind and reverses to 0006. The import-only shell names no unreachable
Comunicacao queue, the policy page states each invitation's route, and the client setup
row shows the invitation message's own delivery state. Verified for real: owner
acknowledgement, refusal, queueing, revoke and reinvite at desktop and mobile; one
accepted fake-SMTP send with a refused duplicate; crash-after-send left uncertain with no
resend; new-account acceptance bound to the contact. Independent review rejected `b53ad6b` on one AA contrast defect in
the new row label; the fix `e400a96` was confirmed **APPROVE**: high confidence, no blockers: all 12 rendered labels measured 7.43:1 in queued and SMTP-accepted states at 1440 and 390, including the cycles-global-killed surface, with assets clean and no new contrast nodes or overflow. Evidence: `.evidence/AXR1-048-happy.txt`,
`AXR1-048-failure.txt`, `AXR1-048-review.md`, `AXR1-048-final-gates-r1.txt`.
This approves T48, not T39 automatic reminders, launch, merge or deployment. Every OG/OD
gate remains open.

`AXR1-039` (policy-authorized automatic reminders) is recorded on delivered `AXR1-048`. An
owner opts in to one fixed rule (`request_reminder`, automatic mode, rule version 1) through a
separate acknowledged policy grant; manual grants, playbook publication and migrations never
create it. A five-minute `sweep_reminders` job enqueues at most one occurrence per overdue
request, eligible contact and local day, in bounded per-tenant batches with resumable cursors,
binding the policy revision and owner seat with `system_operation=sweep_reminders` and no
impersonated session. Obsolete automatic reminders are cancelled before claim, including under
pause or flag changes, and dispatch re-evaluates upload, closure, waiver, expiry and revocation.
The heartbeat shows last attempt, last success, failure code and outcomes on the ops page.
Verified for real: owner opt-in at desktop and mobile; the registered job run twice leaving one
intent; one accepted fake-SMTP send with a refused duplicate; heartbeat on the ops page.
Independent review **APPROVE** at `fdb1bd0`: no blockers; RV1's unchanged close-and-reopen probe now records one SMTP send and one accepted effect, re-acknowledgement is again a visible conflict, and the amendment race, next-day, cancellation, manual-reminder, live-owner and heartbeat checks passed. Evidence:
`.evidence/AXR1-039-happy.txt`, `AXR1-039-failure.txt`, `AXR1-039-review.md` (RV1),
`AXR1-039-review-rv2.md`, `AXR1-039-final-gates-r4.txt`. A first independent review rejected
`e59045f`: an approved same-day close and reopen sent a second reminder for the same request and
contact, because the reminder identity included the cycle amendment. The daily request/contact/day
key is now the enqueue idempotency key (database-unique); a same-day reopen or re-acknowledgement is
reported as a conflict and sends nothing. Regression test kept. This approves T39, not T49 or later follow-ups, launch, merge
or deployment. Every OG/OD gate remains open.

`AXR1-049` (logout recovery after support or tenant denial) is recorded on delivered `AXR1-039`.
When an authenticated account has neither an active membership nor a live support grant for the
firm, the tenant middleware still refuses every route except the registered allauth logout view
(matched by route name and callable), which it admits with no tenant context. Logout keeps its
GET confirmation and POST execution, CSRF and Origin checks, safe local `next` handling and
host-only session cookies; the next account must pass password and MFA again. Verified for real
at desktop and mobile: owner approval and revocation through the actual forms, the revoked
support session and a freshly authenticated revoked account both refused, refused logout POSTs
without a token or with a foreign Origin, logout with an external `next` sent to `/`, a local
`next` honoured, an owner account switch with a new code, and the same recovery after a staff
membership is deactivated. Independent review **APPROVE** at `e681371`: no blockers; all five denial classes reproduced over HTTP, and the real revoked-support and lost-membership journeys at 1440 and 390 recovered through the normal logout and a fresh password plus TOTP sign-in while protected routes stayed 403; 1,145 adversarial probes passed.
Evidence: `.evidence/AXR1-049-happy.txt`, `AXR1-049-failure.txt`, `AXR1-049-review.md`,
`AXR1-049-final-gates-r1.txt`. This approves T49, not T50 or later follow-ups, launch, merge or
deployment. Every OG/OD gate remains open.

`AXR1-050` (support audit actor) is recorded on delivered `AXR1-049`. Every request served under a live
support grant now records the support actor in both audit streams even when the request itself logs that
actor out: the actor is resolved before the handler runs and written after it, including when the handler
fails and rolls back. Requests without a grant still write nothing, and static and health paths never touch
authentication. Verified in a real browser: the owner's support trail names the actor on the logout row at
desktop and mobile, a second actor's rows stay separate, and nothing is recorded after revocation.
Independent review **APPROVE** at `9430bbe`: no blocking findings; 97 regression, 23 attack and 2 desktop/mobile browser tests passed in the reviewer's own lane under the dispatched memory caps, and the exact-head gate artifacts were audited rather than rerun. Evidence: `.evidence/AXR1-050-happy.txt`,
`AXR1-050-failure.txt`, `AXR1-050-review.md`, `AXR1-050-final-gates-r2.txt`, `AXR1-050-final-tail-r1.txt`. This approves T50, not later
follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-044` (ignored late observations) is recorded on delivered `AXR1-050`. The delivery
queue and client-period workspace history distinguish applied, ignored late and superseded
observations. Refused outcomes retain their source time and observed state but have an explicit
ignored qualifier and non-effect explanation; they do not replace current business state.
Monotone state, append-only receipts and frozen bodies are unchanged, with no backfill.
Independent review **APPROVE** at `5cf5957`: no blockers; 94 focused/neighbor tests passed,
25 state/projection attacks passed, and the final browser matrix completed with 54 captures
at 390, 1440 and 1920 pixels under both media preferences. All 48 non-legacy captures had
zero serious/critical axe violations; legacy contrast remains a failure, not an approval.
The reviewer audited the complete r2 gates and fresh JUnit rather than rerunning every suite.
R1's two browser failures led to correcting the existing Journey I exact-text expectations;
an aborted collect-only run overwrote r1's suite JUnit, which remains labeled invalid.
The complete r2 rerun supplies the passing evidence. Evidence: `.evidence/AXR1-044-happy.txt`,
`AXR1-044-failure.txt`, `AXR1-044-review.md`, `AXR1-044-final-gates-r2.txt`.
This approves T44, not later follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-047` (discoverable setup after a flagged CSV import) is recorded on delivered `AXR1-044`.
The import result lists the clients that batch created (in file order, at most 200, disclosed when
capped), each linking to its ordinary page. The client workspace's "Cadastro e autorizações" section
links to the setup page (service, tax profile, contacts and responsible people) only for actors the
setup route admits: the import flag plus the clients.manage capability. The old link that reloaded
the same workspace at `#contatos` is gone. Read-only support, unassigned staff and other firms see no
link and are still refused; flag-off and legacy pages are unchanged. Verified by an owner walking from
the CSV import to the setup page through rendered links only, at desktop and mobile with a keyboard
final step, then adding a contact and queuing its invitation. Independent review **APPROVE** at
`2d3d607`: no blockers; 33 HTTP checks (five actors across four flag states, batch boundaries 0-201
with one query) and 4 browser cases passed, and a restored self-loop failed at the loop assertion.
Pre-existing and out of scope: the legacy client page draws its setup link on the import flag alone,
so read-only support sees a link the route refuses with 403. Evidence:
`.evidence/AXR1-047-happy.txt`, `AXR1-047-failure.txt`, `AXR1-047-review.md`,
`AXR1-047-final-gates-r1.txt`. This approves T47, not later follow-ups, launch, merge or deployment.
Every OG/OD gate remains open.

`AXR1-043` (linked workspace sections) is recorded on delivered `AXR1-047`. In a flagged-on
client-period workspace, Solicitações lists the period's requests with their state, client status and
due date, each linked to its place in the cycle, and Comunicação lists the period's messages with their
current state and receipt history and the publications with the client's receipts, linked to the queue
and the approved effect. Ignored late observations read as history, outcomes stay distinct, empty
periods are typed empty, scoping follows the client already resolved for the page, and support
sessions see subjects masked. Each section costs a fixed number of queries; the whole workspace page
measured 50 queries, which remains the open `AXR1-035` budget work. Independent review **APPROVE** at
`92eaf3a`: no blockers; 175 acceptance/regression tests and 5 extra edge probes passed in the reviewer's
own capped lane, and the gates, negative controls and screenshots were audited. Pre-existing and out
of scope: the support-session queue prints a publication's title. Evidence: `.evidence/AXR1-043-happy.txt`,
`AXR1-043-failure.txt`, `AXR1-043-review.md`, `AXR1-043-final-gates-r1.txt`. This approves T43, not later
follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-040` (unsaved-changes guard) is recorded on delivered `AXR1-043`. The existing CSP-safe
unsavedGuard now covers the product's real editing forms: the four client setup sections, the client and
contact row editors, the new-playbook fields and a draft playbook's step form. Leaving an edited form asks
first, cancelling keeps the fields, a refused save comes back still guarded, and a successful save leaves
without a prompt; pristine, read-only and published forms never ask. Independent review **APPROVE** at
`a81fd85`: no blockers; 10 HTTP, 8 browser and 4 extra desktop/mobile controls passed in the reviewer's own
capped lane, and the gates, negative controls and screenshots were audited. Known limits: the guard clears
on submit before the save succeeds (no product form saves through HTMX yet), and import-only firms stay
unguarded. Evidence: `.evidence/AXR1-040-happy.txt`, `AXR1-040-failure.txt`, `AXR1-040-review.md`,
`AXR1-040-final-gates-r1.txt`. This approves T40, not later follow-ups, launch, merge or deployment. Every
OG/OD gate remains open.

`AXR1-042` (portal next action) is recorded on delivered `AXR1-040`. The R1 portal now chooses the
client's next action from the requests and cycles that client may see: an outstanding request leads the
page and is never contradicted by an instruction to wait, a received request waits for review, an older
period's overdue request is no longer hidden by a newer cycle, and known calendar entries stay a separate,
scoped action. Coverage stays explicitly unknown; nothing turns green from missing data. Internal notes,
reviews and official source assertions stay outside the portal role. Independent review **APPROVE** at
`8f73d27`: no blockers; 22 focused, 272 portal/payment and 14 browser cases at both widths passed in the
reviewer's own capped lane, and the gates, negative controls and screenshots were audited. Evidence:
`.evidence/AXR1-042-happy.txt`, `AXR1-042-failure.txt`, `AXR1-042-review.md`, `AXR1-042-final-gates-r1.txt`.
This approves T42, not later follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-045` (portal documents and account contrast) is recorded on delivered `AXR1-042`. Under R1 the
portal documents and account pages, and the upload form on the documents page, now draw the header email,
footer, document metadata, size hint, empty state and account labels at AA contrast (7.43:1 on white, 7.09:1
on the page background); flag-off and kill-switched pages are byte-identical to before. Independent review
**APPROVE** at `93e0759`: no blockers; 82 focused, 10 axe and 18 live contrast-control cases passed in the
reviewer's own capped lane. The first final-gate run had one browser failure, a Chromium screenshot protocol
error in an unrelated firm-settings test; at the same code it passed alone and the full browser suite passed
168/168. Open: the legacy flag-off contrast (4.46:1, footer 4.26:1) is an owner decision, and the R1 upload
refusal screen (`upload_error.html`) keeps legacy ink as a follow-up. Evidence: `.evidence/AXR1-045-happy.txt`,
`AXR1-045-failure.txt`, `AXR1-045-review.md`, `AXR1-045-final-gates-r1.txt`, `AXR1-045-browser-rerun-r1.txt`.
This approves T45, not later follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-046` (bulk reopening disclosure) is recorded on delivered `AXR1-045`. The bulk approval preview
now shows a frozen reopening as a reopening: its heading and destination differ from a completion, and it
names the preserved completion generation, the affected scope and the unexecuted proposals the reopening
invalidates. Other frozen actions never show reopening effects. The display reads only the sealed payload;
stored manifests, digests and execution are unchanged. Independent review **APPROVE** at `bb1398e`: no
blockers; 133 control, 3 attack and 2 browser cases passed in the reviewer's own capped lane, and the six
screenshots were inspected. The final gates were split by the 15:13 host crash; the reviewer accepted the
completed commands of both runs on byte-identical code. Non-blocking: a stray space before the destination
period ("Reabrir ciclo ."). Evidence: `.evidence/AXR1-046-happy.txt`, `AXR1-046-failure.txt`,
`AXR1-046-review.md`, `AXR1-046-final-gates-r1.txt`, `AXR1-046-final-gates-r3-rest.txt`.
This approves T46, not later follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-036` (cycle detail query budget) is recorded on delivered `AXR1-046`. The populated cycle detail page
now reads its display data in one request-local batch and runs 25 queries on the growth portfolio, down from 50,
within the 25-query target; adding waivers, contacts or obligations no longer adds queries. The completion
checker, permissions and commands still decide live; the batch only feeds the display, and tests prove the
displayed checklist, blockers, waiver validity and evidence-index freshness equal the authoritative answers.
Independent review **APPROVE** at `5cd9d1d`: no blockers; its own count was 25 queries with flat growth, and
4 query, 54 projection/scope, 7 attack and 14 browser cases passed in its own capped lane. The first final-gate
run failed one static guard (a stale exemption pin after the query moved into `display_reads.py`); the pins were
moved test-only and the remaining gates passed on the amended code. The client workspace still measures 50
queries; that is the open `AXR1-035` item. Evidence: `.evidence/AXR1-036-happy.txt`, `AXR1-036-failure.txt`,
`AXR1-036-review.md`, `AXR1-036-final-gates-r1.txt`, `AXR1-036-final-gates-r2.txt`. This approves T36, not later
follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-035` (client workspace query budget) is recorded on delivered `AXR1-036`. The populated client
workspace now loads its sections through one request-local, read-only aggregate and runs 21 queries on the growth
portfolio, down from 50, within the 25-query target; thirty extra requests add no queries. Permissions, the
selected period and every section are unchanged: unassigned or foreign clients still return 404, and the
aggregate never authorizes navigation, other pages or any write. Independent review **APPROVE** at `0b80947`:
no blockers; 21 queries in its own lane, the lookup mutation failed at 52 as intended, and 134 tests, six attacks
and two restored budget cases passed. The first final-gate run failed one static guard (exemption pins moved by
this change and AXR1-036); the pins were updated test-only and the remaining gates passed on the amended code.
Pre-existing and not changed here: the invalid-period 422 is bare text and the flag-off legacy workspace has a
contrast gap. Evidence: `.evidence/AXR1-035-happy.txt`, `AXR1-035-failure.txt`, `AXR1-035-review.md`,
`AXR1-035-final-gates-r1.txt`, `AXR1-035-final-gates-r2.txt`. This approves T35, not later follow-ups, launch,
merge or deployment. Every OG/OD gate remains open.

`AXR1-037` (constrained Hoje latency) is recorded on delivered `AXR1-035`. Hoje no longer reverses a URL and
recomputes the local date for every one of its ~13,350 queue items: each read reverses a route once, keeps the
request's script prefix verbatim and substitutes only the item's UUID (falling back to `reverse()` otherwise), and
computes the local date once. On the constrained class (1 CPU / 400 MiB web, PostgreSQL 320 MiB, Redis 96 MiB) and the
populated growth dataset, an exclusive paired window measured Hoje p95 409.6 ms, then 537.4 ms with only the two
changed files restored, then 426.8 ms (200 requests each, 25 queries every round). This meets the < 500 ms target on
that class; it is not a production SLO. The opt-in, local-only investigation harness refuses non-local targets, never
seeds or analyzes, and deletes every session it issued even when preparation fails partway. Independent review r1
rejected two defects (script-prefix substitution, partial-preparation cleanup); both were fixed with regression tests,
and review r2 **APPROVE** at `aeba3df`: both blockers fixed, independent probes 10/10, final gates confirmed at the
reviewer's exact totals. Earlier windows that overlapped other runs (A3, A4, P3) and P1 (no measurement) are kept on
record and not counted. Evidence: `.evidence/AXR1-037-happy.txt`, `AXR1-037-failure.txt`,
`AXR1-037-paired-window-P4-summary.txt`, `AXR1-037-review.md`, `AXR1-037-review-r2.md`,
`AXR1-037-final-gates-r2.txt`. This approves T37, not later follow-ups, launch, merge or deployment. Every OG/OD gate
remains open.

`AXR1-038` (every-model isolation) is recorded on delivered `AXR1-037`. The behavioral isolation sweep now covers the
seven models it missed - user capability grants, billing accounts, usage records, notification preferences, firm
outbound messages, account exports and their attempts - and an inventory test fails whenever a concrete model has
neither sweep coverage nor an individually justified exemption. Every tenant-composite parent foreign key of the
shipped schema (71 table/column/constraint triples) is now declared in the test suite and reconciled against the
live catalog, so a dropped, renamed or added composite fails instead of turning a cross-tenant probe into a skip.
ClientContact is declared portal NONE in both the roles script and the test registry, with no grant change.
Review history: the first review attempt was lost (no verdict) and kept on record; review r2 rejected the
catalog-only discovery of paired parents, fixed test-only with the declared manifest; review r3 **APPROVE** at
`83e8370`: fresh isolation and scope reproduced (754 passed, 41 skipped), the catalog matched all 71 declared triples, three dropped-FK mutants failed and restored, and the incremental gate coverage was accepted. Full gates last ran at `3b56656` (suite 5103 passed / 41 skipped / 168 deselected,
isolation 720 passed / 41 skipped, CNPJ 29, browser 168); after AXR1-037's fix the affected suites and the browser
reran at `563a34a` (162 and 168 passed), and after the r2 fix isolation plus scope reran at `83e8370` (754 passed, 41
skipped). The default-suite baseline above is not re-rolled by this item because no single full-suite run exists at
its final head; the next full run (final verification) re-establishes it. Evidence: `.evidence/AXR1-038-happy.txt`,
`AXR1-038-failure.txt`, `AXR1-038-review.md`, `AXR1-038-review-r3.md`, `AXR1-038-final-gates-r1.txt`,
`AXR1-038-incremental-r1.txt`, `AXR1-038-isolation-fix.txt`. This approves T38, not later follow-ups, launch, merge or
deployment. Every OG/OD gate remains open.

`AXR1-054` (readable refusals) is recorded on delivered `AXR1-038`. Refusals that used to reach the reader as bare
text or as a Python list - for example the F3 case of opening a cycle for a client still in onboarding - now read as
plain sentences on the product's accessible error page, with unchanged statuses (409/410/422), wording, authorization
order and portfolio-first 404s; in-page, form, flash and stored bulk-item errors read as sentences too, and markup in a
message renders escaped. Real-browser QA passed 12/12 screens at 1440x900 and 390x844 (lang, title, one heading, a
keyboard-reachable way back, no overflow, no serious axe violation). Independent review **APPROVE** at `4c4dace`: no
blockers; its own browser probe passed the F3 path and escaping attacks at both widths. Review-reported residuals for
F4 to classify: a tampered `?revisao_de=` still returns a bare 422, the read-only 403 is plain text, BuildError machine
codes can reach two pages, a caught Http404 gives an empty flash in support views, and bulk-item rows stored before
this change may still hold a repr. Evidence: `.evidence/AXR1-054-happy.txt`, `AXR1-054-failure.txt`,
`AXR1-054-review.md`, `AXR1-054-final-gates-r3.txt`, `AXR1-054-incremental-r1.txt`, `AXR1-054-qa-r4.txt`. This
approves T54, not later follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-034` (named-read and interactive performance evidence) is recorded on delivered `AXR1-054`. The T28/T25 harness
now keeps raw per-request samples (server time, SQL count, gzip HTML bytes) with actor, scope, dataset, head and
resource limits for the six S11.2 reads, Clientes included, and measures real click-to-swapped-content latency for the
Hoje period filter under 9 Mbps / 170 ms RTT mid-tier Android emulation (390x844, 4x CPU). Exclusive capture at
`f3a8afd` on the constrained class (web 1 CPU / 400 MiB, PostgreSQL 320 MiB, Redis 96 MiB, growth dataset seed 42,
200 requests per read, 200 clicks): server p95 Hoje 466.6 ms, Clientes 15.2 ms, exact-name Clientes search 12.2 ms, client workspace 39.4 ms, cycle detail 66.5 ms and portal Inicio 12.8 ms, with at most 23 queries and 12.1 KB gzip HTML on any read. Interactive: click to populated swap p50 748.5 ms, p95 783.1 ms. p50 is the median and p95 nearest rank of the raw samples. The
missing-swap control failed as required with the "missing exact populated HTMX swap" error and reports no percentiles. No read or interaction exceeded a target, so the capture adds no further numbered work. Capture r1 is kept as failed: the lane ran PostgreSQL defaults instead of production's tuning and the database was OOM-killed on Hoje; at production's flags (r3) it was not. Not a production SLO. Review **APPROVE** at
`f3a8afd`: review r1 rejected only a contract misstatement in root's dispatch (median p50); after the correction, r2 confirmed every recomputed median, p95, count, budget and the control. Evidence: `.evidence/AXR1-034-happy.txt`, `AXR1-034-failure.txt`,
`AXR1-034-review.md`, `AXR1-034-final-gates-r1.txt`, `t34-capture-r3/`. This approves T34, not later
follow-ups, launch, merge or deployment. Every OG/OD gate remains open.

`AXR1-041` (stack-aware CI) is recorded on delivered `AXR1-034`. CI now also runs for pull requests into `axr1/**`
branches, checks out each PR's exact head and fails when the checked-out commit differs; PRs into `main` keep merge-ref
testing and `deploy-pilot` still needs a push to `main`. Both pytest steps run on every runner CPU with `pytest-xdist`
(one test database per worker; local STD-GATES stay serial), because R1 roughly doubled the tables each transactional
test flushes and the serial suite was cancelled at its 75-minute ceiling at 53% (run 38537217704). Remote CI at
`f2f3961` (run 38556594907) passed every required job: tests in 60 min 51 s (5149 passed, 41 skipped on 4 workers, the
serial total), container smoke and browser; deploy skipped. Serial STD-GATES at `f2f3961` passed (5149/41/168, isolation
721/41, CNPJ 29, browser 168). Independent review **APPROVE** at `f2f3961`; its notes: the local paused-channel smoke and
failing control ran at `f85313d` (the exact-head smoke is the remote job), the test job's margin under its ceiling is 14
minutes, and the into-`main` path is verified by reading only. The plan's integrated-identity condition is met only by
the top repair PR's CI run, so item 41 stays unticked until that run is green. Evidence: `.evidence/AXR1-041-happy.txt`,
`AXR1-041-failure.txt`, `AXR1-041-review.md`, `AXR1-041-final-gates-r1.txt`, `AXR1-041-remote-ci-r2-test-job.log`.
Every OG/OD gate remains open.

`AXR1-055` (saved dates in date inputs) is recorded on delivered `AXR1-041`. Edit forms rendered a saved date in its
localized display format inside `<input type="date">`, which browsers treat as empty, so re-saving an engagement silently
cleared its dates (final F3 N1). A new `input_value` filter renders each field in the input's own wire format (ISO for
dates) through the shared field partial; submitted strings and non-date values are unchanged. Real-browser QA passed 4/4
(pt-BR and en at 1440x900 and 390x844): the editor shows both saved dates, changing only the service kind keeps them, and
reopening shows them again. Final STD-GATES at `28b2eb0` passed (5156/41/168, isolation 721/41, CNPJ 29, browser 168).
Independent review **APPROVE** at `28b2eb0`: patch identity, 24 focused tests and four browser attacks. Evidence:
`.evidence/AXR1-055-happy.txt`, `AXR1-055-failure.txt`, `AXR1-055-review.md`, `AXR1-055-final-gates-r2.txt`. Every OG/OD
gate remains open.

`AXR1-056` (portal empty-state contrast) is recorded on delivered `AXR1-055`. The empty portal Pagamentos state now uses
T45's `text-ink-600` treatment for R1 tenants (final F3 N2 measured 4.46:1); non-R1 bytes are unchanged and the portal axe
sweep covers `/pagamentos/` at both widths. The item's acceptance was corrected after review r1: the portal Inicio
no-company page renders only on a host that resolves no active tenant (an active R1 tenant without a client seat gets
403), so it is recorded under deferred T77 (4.46:1 card and header e-mail, 4.27:1 footer), not claimed fixed. Final
STD-GATES at `dd9c66a` passed (5159/41/168, isolation 721/41, CNPJ 29, browser 168). Independent review r2 **APPROVE** at
`dd9c66a`. Evidence: `.evidence/AXR1-056-happy.txt`, `AXR1-056-failure.txt`, `AXR1-056-review.md`,
`AXR1-056-review-r2.md`, `AXR1-056-final-gates-r3.txt`. Every OG/OD gate remains open.

`AXR1-058` (completion execution refusal) is recorded on delivered `AXR1-056`. An approved completion whose cycle was not yet
in "Em execução" was refused with the bare "Esta transição não é permitida."; the refusal now names the required state and
the path there ("Próximo estado" through "Em revisão", "Aguardando aprovação" and "Em execução"), with the status code,
authorization order and every transition rule unchanged, and the same approved completion then executes. Final
STD-GATES at `dbd381d` passed (5160/41/168, isolation 721/41, CNPJ 29, browser 168). Independent review **APPROVE** at
`dbd381d`: patch identity, 142 tests, desktop/mobile recovery and terminal-cycle 409 attacks. Evidence:
`.evidence/AXR1-058-prep.md`, `AXR1-058-review.md`, `AXR1-058-final-gates-r3.txt`. Every OG/OD gate remains open.

`AXR1-059` (refusal pages' way out and product 404) is recorded on delivered `AXR1-058`. Every user-reachable 403 and 404
found now renders the product refusal layout in pt-BR with a title, one heading, a keyboard-reachable way back and, when
signed in, a working Sair: the support-revoked, wrong-host and ended-portal 403s, a new product 404 (identical for foreign
and nonexistent objects), Django's CSRF-failure page (now "Página expirada"), the read-only and scan-refused 403s, and every
invite refusal page. Status codes, authorization order, CSRF refusal and portfolio-first 404s are unchanged (review
measured 15 responses on parent and head). On firm-door invite refusal pages the legacy platform-host footer still fails
contrast (4.27:1); a control proves it predates this item, so it is recorded under deferred T77, not claimed fixed. Final
STD-GATES at `0055ad3` passed (5176/41/168, isolation 721/41, CNPJ 29, browser 168). Independent review **APPROVE** at `0055ad3` (r1 plus a delta review of the
amendment). Evidence: `.evidence/AXR1-059-prep.md`, `AXR1-059-review.md`, `AXR1-059-final-gates-r1.txt`. Every OG/OD gate
remains open.

`AXR1-060` (portal next action for awaiting publications) is recorded on delivered `AXR1-059`. A publication awaiting the
client's confirmation is now the portal's next action, its confirm link works and it clears afterwards; migration
`0019_portal_receipt_lookup` grants the portal role read access to four receipt columns only, and existing row-level
security still limits rows to the client's own. The first final gates found two defects the reviews missed: the column-only portal grant was not declared where the
isolation guard reads portal decisions, and an annotation was named "pending"; the decision is now declared and asserted
(SELECT * refused, only the declared columns readable, RLS confining rows) and the annotation renamed. Final STD-GATES at `da27d06` passed (5182/41/168, isolation 722/41, CNPJ 29, browser 168). Independent review
**APPROVE** at `da27d06`: 32 tests, the live confirmation flow, and cross-client and cross-tenant attacks. Evidence:
`.evidence/AXR1-060-prep.md`, `AXR1-060-review.md`, `AXR1-060-final-gates-r1.txt`. Every OG/OD gate remains open.

`AXR1-061` (email metering states) is recorded on delivered `AXR1-060`. The account page now tells missing, partial and
measured-zero email metering apart ("Sem medição · Apuração de e-mails pendente" instead of 0). The current month always
reads as partial because usage is recorded for the prior day; review judged that consistent with the metering design.
Final STD-GATES at `6521752` passed (5190/41/168, isolation 722/41, CNPJ 29, browser 168). Independent review **APPROVE** at `6521752`: 30 tests and 10 browser cases.
Evidence: `.evidence/AXR1-061-prep.md`, `AXR1-061-review.md`, `AXR1-061-final-gates-r1.txt`. Every OG/OD gate remains
open.

`AXR1-062` (R1 client flows in pt-BR) is recorded on delivered `AXR1-061`. On active R1 portal hosts the client invitation,
sign-in, re-authentication, recovery and request/upload flows render in pt-BR whatever the browser language, including
the retry after a refused upload; other hosts keep English negotiation. After final F3 found the invitation's
password-mismatch error still in English (F3R2-01), the message became "Os dois campos de senha não correspondem.", with
a test under both headers. Final STD-GATES at `6e316f6` passed (5207/41/168, isolation 722/41, CNPJ 29, browser 168). Independent review **APPROVE** at `6e316f6` (r1
plus delta reviews of both amendments). The first final gates showed the switch's per-request tenant lookup broke the
`/readyz` and `/versionz` probe contracts on portal hosts; it now honours `@transaction.non_atomic_requests`, so probes
never touch the database. Evidence: `.evidence/AXR1-062-prep.md`, `AXR1-062-review.md`,
`AXR1-062-final-gates-r3.txt`. Every OG/OD gate remains open.

`AXR1-063` (upload-refusal ink) is recorded on delivered `AXR1-062`. The R1 portal upload-refusal (400) and retry pages now
carry the same AA ink as the successful vault form; the status stays 400, the retry keeps CSRF, multipart encoding and the
size limit, and flag-off pages are byte-identical. Final STD-GATES at `fc53fd9` passed (5213/41/170, isolation 722/41, CNPJ 29, browser 170). Independent review
**APPROVE** at `fc53fd9`: 76 HTTP and 6 browser tests including two attacks. Residual for F4: a file rejected for its type
still shows "Nenhum arquivo foi escolhido". Evidence: `.evidence/AXR1-063-happy.txt`, `AXR1-063-failure.txt`,
`AXR1-063-review.md`, `AXR1-063-final-gates-r2.txt`. Every OG/OD gate remains open.

`AXR1-064` (playbook bound form) is recorded on delivered `AXR1-063`. A refused playbook creation (422 or 409) now re-renders
the bound form with the submitted values, selected options and inline errors, and correcting only the key succeeds. The
shared field partial carries both this change and item 55's date values (a one-line conflict resolved during placement and
verified by review). Final STD-GATES at `0ebaeee` passed (5222/41/170, isolation 722/41, CNPJ 29, browser 170). Independent review **APPROVE** at `0ebaeee`: 70 tests,
desktop and mobile retries, bound and unbound select and date probes. Evidence: `.evidence/AXR1-064-prep.md`,
`AXR1-064-review.md`, `AXR1-064-final-gates-r2.txt`. Every OG/OD gate remains open.

`AXR1-057` (evidence export with unscanned originals) is recorded on delivered `AXR1-064`. A cycle's evidence export now
succeeds when some originals were never scanned: unsafe, unscanned or rejected originals are excluded and named with their
reason on the page and in the archive, and an all-clean archive is byte-identical to before. Review r1 found a race (an
original rejected after the claim reached a READY archive); eligibility is now rechecked under row locks in the
publication transaction, historical exports included, and unsafe attempts never become READY. Migration
`0020_evidenceexport_excluded_originals` follows item 60's `0019`. Final STD-GATES at `d1e803f` passed (5240/41/170, isolation 722/41, CNPJ 29, browser 170 on a same-head rerun).
Independent review r2 **APPROVE**: the rebuilt race attack fails on the old head and passes here, plus two new attacks;
the r3 delta review confirmed it at `d1e803f` (product code unchanged, test-file typing only). Residual for F4: an archive
published before a later rejection stays downloadable. Evidence: `.evidence/AXR1-057-prep.md`, `AXR1-057-review.md`,
`AXR1-057-review-r2.md`, `AXR1-057-review-r3.md`, `AXR1-057-final-gates-r3.txt`, `AXR1-057-browser-rerun-r1.txt`. Every
OG/OD gate remains open.

`AXR1-004` gate repairs (`2f4cf92`) add a locked membership grant revision and
409 conflict feedback, restore the original owner query budgets, and mirror the
portal NONE decision. Reversing the R1 seed removes only R1 grants (active and
revoked), retaining audit history and non-R1 data; reapplying does not restore
removed authority. Evidence: `.evidence/AXR1-004-fix-1.txt` through `-fix-5.txt`,
`-fix-repeat.txt`, `-fix-mutations.txt`, `-fix-browser.txt`, and `-fix-gates.txt`.

## Plan index

| Plan | Status | Basis |
| --- | --- | --- |
| `axorim-regional-mvp` | **current** (product direction) | Approved 2026-10-22 on top of the delivered R1 release. 42 todos in seven waves plus a final verification wave, merged per wave on the owner's authorization; `AXRM-001` recorded the decisions and gate rows; `AXRM-004` recorded the final-head performance baseline and the regional read budgets. |
| `axorim-pivot-r1` | **delivered** (merged 2026-10-15) | Started 2026-09-27. 29 todos in five waves plus a final verification wave; `AXR1-001` recorded the pivot. Release `00f1d41`, followed by backup fix `71225ef` on 2026-10-21; the regional MVP is now current. Pilot-authorization carry-forward remains OD-02. |
| `pilot-readiness-v2` | **historical** (authorized for `a962173` only) | All 41 execution rows recorded closed; F1-F4 recorded complete in the v2 plan. `pilot_start_authorized=2026-09-26` covers `a962173` (tag `v0.2.0-rc1`), not `71225ef` or the regional MVP. AXRM-002's read-only checker exits 1 for S1, S2, S3, S18, S20; OG-02 remains open pending the owner's OD-02 carry-forward-or-re-run decision. |
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

**AXR1 uses a linear stack of open pull requests:** the first targets `main`, and
each later PR targets the previous `axr1/*` branch. Merge only on the owner's
instruction. CI evidence must identify the exact head tested; missing checks
remain explicitly unverified.

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
old_host_termination_not_before=2026-10-03T15:48:06Z (v0.2.0-rc1 tag 2026-09-26T15:48:06Z + 7 days; owner action, not executed by AXRM-002)

`freeze_sha` is the merged row-27 commit, backfilled from `.evidence/PILOT2-404E-happy.txt`
and repeated in `.evidence/PILOT2-404-operator.txt`. The `window_utc` and
`rollback_deadline` lines are the plan as first recorded. The owner then moved the
cutover forward a day, and the `executed_*` lines are what ran
(`.evidence/PILOT2-404-operator.txt`, `.evidence/PILOT2-404O2-operator.txt`). The OCI
host was recorded stopped with its disk intact on 2026-09-23
(`.evidence/PILOT2-401-operator.txt`); this is historical, not a current host probe.
Tag `v0.2.0-rc1` now exists at `a962173`, with tagger time
2026-09-26T12:48:06-03:00 (15:48:06 UTC);
`/home/samarone/Documents/app_mei-pilot2/.evidence/PILOT2-505-operator.txt:1-3`
records `retention_window_start=2026-09-26`. The seven-day minimum has elapsed,
but does not prove termination, wiping or credential rotation happened. Those
remain owner/operator actions under v2 row 38 Part 2; AXRM-002 neither executed
them nor changed the old host. `ops/RELEASES.md` remains the frozen preparation
template, not evidence that the tag is still absent.

The authoritative 12-step cutover table, with its as-executed record, is the ordered
state machine under "Cutover: data freeze, restore, and DNS flip" in `ops/README.md`.

## Suite baseline

AXRM-002 is documentation-only. Before the change at `014123f`, the named
non-database suites passed: `uv run pytest tests/scope/ -q` **33 passed** and
`uv run pytest tests/ops/ -q` **27 passed**. Evidence:
`.evidence/AXRM-002-gates.txt`. No tests or implementation changed; the historical
full-suite figures below are not a new full-suite run for this reconciliation.

The full test suite is the gate at every pull-request head and at the final
implementation head. The current default-suite baseline is **5148 passing** (41 skipped, 168 deselected), measured at `AXR1-034` code `133793b`, the **5137** below plus **11** cases. The reviewed head `f3a8afd` adds only the driver CPU-limit repair with its one test (static clean, targeted 304 passed) and the docs-only placement on `AXR1-054`. Isolation (**721 passed, 41 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**168 passed**) passed at `133793b`. Evidence: `.evidence/AXR1-034-final-gates-r1.txt`, `AXR1-034-static-r3.txt`, `AXR1-034-targeted-r3.txt`.
This status-only delivery changes no reviewed implementation.

The previous default-suite baseline was **5137 passing** (41 skipped, 168 deselected), measured at `AXR1-054` code `a7830c7`; it includes AXR1-038's isolation sweep, whose skips raise the skipped count. The placed head `4c4dace` differs only in this file and AXR1-038's two review-fix isolation test files, which pass there (isolation + scope 754 passed, 41 skipped). Isolation (**720 passed, 41 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**168 passed**) passed at `a7830c7`. Evidence: `.evidence/AXR1-054-final-gates-r3.txt`, `AXR1-054-incremental-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **5032 passing** (30 skipped, 168 deselected) at `AXR1-037` code `aeba3df` (the reviewed head), the **5025** below plus **7** cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**168 passed**) passed at `aeba3df`. Evidence: `.evidence/AXR1-037-final-gates-r2.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **5025 passing** (30 skipped, 168 deselected) at `AXR1-035`: the first run at `8e3ef88` passed 5024 with one static-guard failure, fixed test-only at `0b80947` (the reviewed code), where all 33 scope tests pass; that is the **5008** below plus **17** cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**168 passed**) passed at `0b80947`. Evidence: `.evidence/AXR1-035-final-gates-r1.txt`, `AXR1-035-final-gates-r2.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **5008 passing** (30 skipped, 168 deselected) at `AXR1-036`: the first run at `1c3301a` passed 5007 with one static-guard failure, fixed test-only at `8ee7ce0` (code-identical to the reviewed `5cd9d1d`), where all 33 scope tests pass; that is the **4984** below plus **24** cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**168 passed**) passed at `8ee7ce0`. Evidence: `.evidence/AXR1-036-final-gates-r1.txt`, `AXR1-036-final-gates-r2.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4984 passing** (30 skipped, 168 deselected), verified at `AXR1-046` code `dcc8ce1` (code-identical to the reviewed `bb1398e`) on `AXR1-045`: the **4977** below plus **7** new cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**168 passed**) passed across two runs split by a host crash, all on code-identical heads. Evidence: `.evidence/AXR1-046-final-gates-r1.txt`, `AXR1-046-final-gates-r3-rest.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4977 passing** (30 skipped, 168 deselected), verified at `AXR1-045` code `978dbd8` (code-identical to the reviewed `93e0759`) on `AXR1-042`: the **4968** below plus **9** new cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy and migration drift passed; browser passed **168** on a same-code rerun after one Chromium screenshot protocol error. Evidence: `.evidence/AXR1-045-final-gates-r1.txt`, `AXR1-045-browser-rerun-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4968 passing** (30 skipped, 164 deselected), verified at `AXR1-042` code `8f73d27` on delivered `AXR1-040` head `bc11afc`: the **4946** below plus **22** new cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**164 passed**) passed serially. Evidence: `.evidence/AXR1-042-final-gates-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4946 passing** (30 skipped, 164 deselected), verified at `AXR1-040` code `a81fd85` on delivered `AXR1-043` head `aefb7ca`: the **4936** below plus **10** new cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**164 passed**) passed serially. Evidence: `.evidence/AXR1-040-final-gates-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4936 passing** (30 skipped, 156 deselected), verified at `AXR1-043` code `92eaf3a` on delivered `AXR1-047` head `33a4da0`: the **4928** below plus **8** new or reshaped cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**156 passed**) passed serially. Evidence: `.evidence/AXR1-043-final-gates-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4928 passing** (30 skipped, 156 deselected), verified at `AXR1-047` code `2d3d607` on delivered `AXR1-044` head `b965b8b`: the **4921** below plus **7** new or reshaped cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**156 passed**) passed serially. Evidence: `.evidence/AXR1-047-final-gates-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4921 passing** (30 skipped, 154 deselected), verified at `AXR1-044` code `5cf5957` on delivered `AXR1-050` head `5a42438`: the **4911** below plus **10** new or reshaped cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**154 passed**) passed serially. Evidence: `.evidence/AXR1-044-final-gates-r2.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4911 passing** (30 skipped, 154 deselected), verified at `AXR1-050` code `9430bbe` on delivered `AXR1-049` head `7464dd1`: the **4902** below plus **9** new or reshaped cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**154 passed**) passed serially. Evidence: `.evidence/AXR1-050-final-gates-r2.txt` and `AXR1-050-final-tail-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4902 passing** (30 skipped, 154 deselected), verified at `AXR1-049` code `e681371` on delivered `AXR1-039` head `778b5f1`: the **4893** below plus **9** new or reshaped cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**154 passed (r2; r1 had 1 Chromium screenshot-capture error, see failure evidence)**) passed serially. Evidence: `.evidence/AXR1-049-final-gates-r1.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4893 passing** (30 skipped, 154 deselected), verified at `AXR1-039` code `fdb1bd0` on delivered `AXR1-048` head `0fccec1`: the **4855** below plus **38** new or reshaped cases. Isolation (**642 passed, 30 skipped**), CNPJ (**29 passed**), build with a clean static diff, lint, format, Mypy, migration drift and browser (**154 passed**) passed serially. Evidence: `.evidence/AXR1-039-final-gates-r4.txt`.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4855 passing** (30 skipped, 154 deselected),
verified at `AXR1-048` code `b53ad6b` on delivered `AXR1-053` head `d87cf17`: the
**4803** below plus **52** new or reshaped import-origin cases. Isolation (**640**, with
**30** justified skips), CNPJ (**29**), build with a clean static diff, lint, format, Mypy
and migration drift passed serially; browser (**154**) passed at `e400a96`, which changes
only one template class. Evidence: `.evidence/AXR1-048-final-gates-r1.txt` and
`AXR1-048-final-browser-r1.txt`. Earlier failed targeted runs (Mypy, migration syntax,
one test artifact) remain separate failures with their repairs recorded.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4803 passing** (30 skipped, 154 deselected),
verified at `AXR1-053` code `f0a2d9e` on delivered `AXR1-033` head `657ede8`:
the **4722** below plus **81** new notice-authority and compatibility cases.
Isolation (**640**, with **30** justified skips), CNPJ (**29**), build with a clean
static diff, lint, format, Mypy and migration drift passed serially; browser (**154**)
passed at `136a267`, which differs only in `tests/browser/test_onboarding.py`.
Evidence: `.evidence/AXR1-053-final-gates-r3.txt` and `AXR1-053-final-browser-r1.txt`.
Earlier failed full runs (63 failed with 11 errors, then 2, then 2 browser failures)
remain separate failures with their repairs recorded, not passing evidence.
This status-only delivery changes no reviewed implementation.

Before that, the default-suite baseline was **4722 passing** (28 skipped, 146 deselected),
verified at `AXR1-033` code `c6653b0` on delivered `AXR1-032` head `65392c6`:
the **4693** below plus **29** new invitation regression cases.
Isolation (**628**, with **28** justified skips), CNPJ (**29**), browser (**146**),
lint, format, Mypy, migration drift and clean-tree checks passed serially.
Build/static was not applicable under the plan's conditional rule: no assets changed.
Evidence: `.evidence/AXR1-033-gates-root3.txt`, all applicable exits zero and
`T33_ALL_STD_GATES_OK` at the same head. Interrupted overlapping runs and the
earlier two-regression full run remain separate failures, not passing evidence.
Independent review retained these gates and passed 71 focused checks.
This status-only delivery changes no reviewed implementation.

The previous default-suite baseline was **4693 passing** (28 skipped, 146 deselected),
verified at `AXR1-032` code `f4e8c87` on delivered `AXR1-031` head `1bec8d6`:
the **4683** below plus **10** new authority regression cases.
Isolation (**628**, with **28** justified skips), CNPJ (**29**), browser (**146**),
lint, format, Mypy, migration drift, rebuilt-static equality and clean-tree checks passed.
Evidence: `.evidence/AXR1-032-gates.txt`, all ten exits zero and final `FAILED=0`.
Independent review audited the integrated gates and passed its own 14 focused checks.
This status-only delivery commit changes no reviewed implementation.

The previous default-suite baseline was **4683 passing** (28 skipped, 146 deselected),
verified at `AXR1-031` code `bc2c0cb` on delivered `AXR1-029` head `ff372a6`:
the **4673** below plus **10** new default-suite cases.
The serial run passed isolation (**628**, with **28** justified skips), CNPJ (**29**),
browser (**146**), lint, format, mypy, migration drift, rebuilt-static equality and clean-tree checks.
Evidence: `.evidence/AXR1-031-gates.txt`, all ten exits zero with matching begin/end head.
Independent review audited the completed run and passed its own focused real-surface checks.

The previous default-suite baseline was **4673 passing** (28 skipped, 138 deselected),
verified at `AXR1-029` code `46e1eed` on delivered `AXR1-026` head `76bdd55`:
the **4654** below plus **19** net-new default-suite cases.
The same serial run passed isolation (**628**, with **28** justified skips), CNPJ (**29**),
browser (**138**), lint, format, mypy, migration drift, rebuilt-static equality and the clean-tree check.
Evidence: `.evidence/AXR1-029-final-gates2.txt`, with all ten exits zero and a matching final-head marker.
Independent review audited those broad results and ran its own focused mechanism, browser and mutation
checks. This status-only commit records the verified candidate without changing its implementation.

The previous default-suite baseline was **4654 passing** (28 skipped, 134 deselected),
verified at `AXR1-026` code `80fe953`: the **4336** below plus **318** net-new passing cases.
Lint, format, mypy, migration drift and the clean-tree check passed at that head.
Isolation (**628** passing, **28** skips), CNPJ (**29**), browser (**134**) and rebuilt-static equality
passed at `58be395`; independent review also reran isolation at `80fe953`.
The only intervening change placed an existing approval comment next to its query, with identical
executable ASTs. The earlier full-suite source-guard failure remains recorded; the complete rerun passed.
Evidence: `.evidence/AXR1-026-final-full.txt`, `AXR1-026-gates-r3.txt` and
`AXR1-026-marker-reconciliation.md`. Delivery replayed the same code onto the `AXR1-025` status commit
`6e8d511`.

The previous default-suite baseline was **4336 passing** (134 deselected), verified at
`AXR1-025` code `439fd67` on `AXR1-023` code `b02cf0e`: the **4330** below plus **6** net-new default-suite
cases. The same serial run passed isolation (**312**), CNPJ (**29**), browser (**134**, 32 more than the
**102** below), lint, format, mypy and migration drift. Its static and clean-tree checks failed only on a
utility token in inherited status prose; after the one-line prose fix both passed on the corrected head.
Evidence: `.evidence/AXR1-025-gates-final.txt`.

The previous default-suite baseline was **4330 passing** (102 deselected), verified at
`AXR1-023` code `b02cf0e`: the **4280** below plus **50** net-new default-suite cases.
The same head passed isolation (**312**), CNPJ (**29**), browser (**102**), lint, format,
mypy, migration drift, build/static equality and the clean-tree check. The first full-suite
attempt lacked `SUPERUSER_DATABASE_URL`; only that gate was rerun with the corrected lane
setting, without changing code or tests. Evidence: `.evidence/AXR1-023-fix-gates.txt` and
`AXR1-023-fix3-full-rerun.txt`.

The previous default-suite baseline was **4280 passing** (94 deselected), verified at placed
`AXR1-028` code `6bdbd41` on `AXR1-027` code `c1451a2`: the **4253** below plus **27** net-new
default-suite cases. The same serial run passed isolation (**309**), CNPJ (**29**), browser
(**94**), lint, format, mypy, migration drift and a clean rebuilt-static check. Evidence:
`.evidence/AXR1-028-gates-placed.txt`.

The previous default-suite baseline was **4253 passing** (94 deselected), verified at placed
`AXR1-027` code `c1451a2` (tree `40373cd`) on `AXR1-024` code `cd19380`: the **4204** below plus **49** net-new default-suite cases. The same serial run passed isolation
(**309**), CNPJ (**29**), browser (**94**), lint, format, mypy, migration drift and a
clean rebuilt-static check. Evidence: `.evidence/AXR1-027-gates-placed.txt`.

The previous default-suite baseline was **4204 passing** (92 deselected), verified at
`AXR1-024` code `cd19380` (tree `c75f2a5`) on `AXR1-022` repaired code `af8e053`: the
**4072** below plus **132** net-new default-suite cases. The same serial
run passed isolation (**309**), CNPJ (**29**), browser (**92**), lint, format,
mypy, migration drift and a clean rebuilt-static check. Evidence:
`.evidence/AXR1-024-gates-final.txt`.

The previous default-suite baseline was **4072 passing** (92 deselected),
verified at repaired `AXR1-022` code `af8e053` (tree `a5dbdfd`) on `AXR1-021`
code `d8963b8`: the **3918** below plus **154** net-new
default-suite cases. The same serial run passed isolation (**306**), CNPJ
(**29**), browser (**92**), lint, format, mypy, migration drift and a clean
rebuilt-static check. The placed head `6a78683` had passed the same gates (4072,
isolation 306, CNPJ 29, browser 90). Evidence: `.evidence/AXR1-022-fix-gates.txt`,
`.evidence/AXR1-022-gates-placed.txt` and the reviewers'
`T22-RV-gates.txt` and `T22-RV2-confirm.md`.

The previous default-suite baseline was **3918 passing**
(86 deselected), verified at placed `AXR1-021` code `d8963b8` (tree `a294f13`) on
`AXR1-020` repaired code `549d202`: the **3889** below plus **29** net-new
default-suite cases. The same serial run passed isolation (**288**), CNPJ
(**29**), browser (**86**), lint, format, mypy, migration drift and a
clean rebuilt-static check. The confirmation reviewer audited the repair's
identical run at `51afd08` and reran the search and palette tests. Evidence:
`.evidence/AXR1-021-gates-place3.txt` and `.evidence/T21-RV2-confirm.md`.

The previous default-suite baseline was **3889 passing**
(78 deselected), verified at `AXR1-020` repaired code `549d202` (tree
`f9ee6d4`), on `AXR1-019` repaired code `3c8e5e2`: the **3826** below plus
**63** net-new default-suite cases. The same serial run passed isolation
(**282**), CNPJ (**29**), browser (**78**), lint, format, mypy,
migration-drift and a clean rebuilt-static check. The confirmation
reviewer reran lint, format, mypy and migration drift, the bulk and delivery
modules (118), isolation (282), the bulk browser tests (4) and the static
rebuild, all green. Evidence: `.evidence/AXR1-020-fix2-gates.txt` and
`.evidence/T20-RV3-gates.txt`.

The previous default-suite baseline was **3826 passing**
(74 deselected), verified at `AXR1-019` repaired code `3c8e5e2` (tree
`e734580`), on final `AXR1-018` code `cae2ba6`: the **3721** below plus
**105** net-new default-suite cases. The same serial run passed isolation
(**276**), CNPJ (**29**), browser (**74**), lint, format, mypy,
migration-drift and a clean rebuilt-static check. The confirmation reviewer
audited that run and reran 500 targeted tests, isolation (**276**), the six
`AXR1-019` browser cases and the restored scale test. Evidence:
`.evidence/AXR1-019-fix-gates.txt` and `.evidence/T19-RV2-gates.txt`.

The previous default-suite baseline was **3721 passing**
(68 deselected), independently verified at `AXR1-018` code `cae2ba6` (tree
`5c16466`), on final `AXR1-017` code `1902cf7`: the **3558** below plus
**163** net-new default-suite cases. The same serial review passed isolation
(**270**), CNPJ (**29**), browser (**68**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Evidence: `.evidence/AXR1-018-rv-gates.txt`
and `.evidence/AXR1-018-gates-placed.txt`.

The previous default-suite baseline was **3558 passing**
(68 deselected), verified at `AXR1-017` code `1902cf7` (tree `146169b`), on
final `AXR1-016` (`a3a8b4b`): the **3438** below plus **120** net-new
default-suite cases. The same serial run passed isolation (**245**), CNPJ
(**29**), browser (**68**), lint, format, mypy, migration-drift and a clean
rebuilt-static check, and the independent confirmation review checked that
log. Evidence: `.evidence/AXR1-017-n1-full-gates.txt`.

The previous default-suite baseline was **3438 passing**
(60 deselected), independently verified at `AXR1-016` code `75272c6`
(tree `ab532c7`), on final `AXR1-015` (`3d519e7`): the **3372** below plus
**66** net-new default-suite cases. The same serial review passed isolation
(**245**), CNPJ (**29**), browser (**60**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Evidence: `.evidence/AXR1-016-rv-std.txt`.

The previous default-suite baseline was **3098 passing**
(50 deselected), independently verified at `AXR1-014` code `c956af9`
(tree `6c3fb90`), on final `AXR1-013` (`0382ddf`): the **3029** below plus
**69** net-new default-suite cases. The same serial review passed isolation
(**225**), CNPJ (**29**), browser (**50**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Four additional browser cases performed
**36** natural evidence journeys. Evidence: `.evidence/AXR1-014-rv-final.txt`;
independent mutations, populated migration and revocation proofs are retained
with that task's evidence. The final runner recorded gate and cleanup exit 0.
This status-only commit does not change the approved code.

The previous default-suite baseline was **3029 passing**
(46 deselected), independently verified at `AXR1-013` code `8f5f475`
(tree `7f93ff2`), on final `AXR1-012` (`8bf17e3`): the **2956** below plus
**73** net-new default-suite cases. The same serial review ran isolation
(**222**), CNPJ (**29**), browser (**46**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Evidence:
`.evidence/AXR1-013-rv-8f5f475-baseline.txt`; the separate biting mutation,
exact restore, two passing keyboard cases and teardown are recorded in
`-mutation.txt`, `-restored.txt` and `-cleanup-final.txt` with that prefix.
This status-only commit does not change the approved code.

The previous default-suite baseline was **2956 passing**
(44 deselected), verified by an independent serial full run of `AXR1-012` placed on
the final `AXR1-011` head (`4772761`): the **2895** of `AXR1-011` plus **61**
net-new `AXR1-012` cases. The same review ran the browser suite (**44**), isolation
(**216**), the CNPJ pack (**29**), `npm run build` with a clean `static/` diff and
`docker compose -f docker-compose.prod.yml config` with and without the `clamav`
profile. This status-only commit does not change the tested code.

The previous default-suite baseline was **2895 passing**
(44 deselected), verified by one serial full run of `AXR1-011` placed on the
final `AXR1-010` head `9aa5dbc` (code at `ce92e78`). It is the **2777** of
`AXR1-010` plus **118** net-new `AXR1-011` cases covering the requests inbox,
upload and relink, the portal-invite outbox, reminder re-evaluation, the company
switcher, branding and the portal journey. Before placement the same code measured
**2893** on the `AXR1-010` feature commit `e184bc4` (2775), in the same lane
(`.evidence/AXR1-011-baseline.txt` and `-gates.txt`). On the placed code the separate
browser run passed **44**, isolation **216**, the CNPJ pack **29**, and the delivery,
portal and cycle subset **497** (`.evidence/AXR1-011-placement-gates.txt`). This
status-only commit does not change the tested code.

The previous default-suite baseline was **2777 passing**
(42 deselected), verified by one serial full run on the `AXR1-010` code
(`54e76ad`). It is the **2669** collected on final T9 (`a521e50`: the 2667 below
plus two replay fast-path regressions) plus **108** net-new `AXR1-010` cases:
72 in the new delivery, phased-task and outbox-hygiene modules, and 36 from
existing table-enumerating meta-tests that now cover the four delivery tables.
The separate browser run passed **42**, isolation **180** and the CNPJ pack
**29**. Evidence: `.evidence/AXR1-010-fix-gates.txt` and `-fix-1.txt`. This
status-only commit does not change the tested code.

The previous default-suite baseline was **2667 passing**,
verified by one serial full run on the placed and repaired `AXR1-009` code
(`7d9db04`): the **2478** T8 baseline plus **189** net-new cases covering cycle
transitions, generation/replay, isolation and model inventories, import rollback
and flag-off compatibility. The separate browser run passed **40**, isolation
**168** and the CNPJ pack **29** (`.evidence/AXR1-009-placement-gates.txt`).

The previous default-suite baseline was **2478 passing**,
verified by one serial full run on the placed `AXR1-008` code (`6de1fc2`):
the **2407** below plus **71**
net-new `AXR1-008` tests (the versioned-playbook services, adversarial probes,
HTTP conflict matrix and formset regressions, plus the cycles-table portal
coverage cases). The browser run passed **36**, isolation **148** and the CNPJ
pack **29** (`.evidence/AXR1-008-placement.txt`).

The previous baseline was **2407 passing**,
verified by one serial full run on the placed `AXR1-007` code (`2a15498`): the
**2329** below plus **78**
net-new `AXR1-007` tests (the import wizard, onboarding screens, contact-invite
delivery and flag-agreement modules, including the gate-review regressions).
The browser run passed **30**, isolation **139** and the CNPJ pack **29**
(`.evidence/AXR1-007-placement-gates.txt`).

The previous baseline was **2329 passing**,
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
`browser` job. The suite now contains **94** cases: the **24** passing at the T3
commit `bff2bb2`, plus the two-viewport reviewer-conflict regression, plus the
four `AXR1-007` onboarding and import-wizard cases (two journeys at two
viewports), plus the six `AXR1-008` playbook cases (editor keyboard reorder and
preview-bound publication journeys at two viewports), plus the four `AXR1-009`
workspace/portal and distinct-client context cases (two journeys at two
viewports), plus the two `AXR1-010` Comunicação queue cases (one journey at two
viewports), plus the two `AXR1-011` portal-upload/staff-request cases, plus the
two `AXR1-013` document-review cases, plus the four `AXR1-014` exception cases,
plus the eight `AXR1-015` approval-card cases, plus the two `AXR1-016`
publication-receipt cases, plus the eight `AXR1-017` coverage-panel cases
(four journeys at two viewports), plus the six `AXR1-019` briefing cases
(two briefing-state journeys and one long-client-name journey at two
viewports), plus the four `AXR1-020` bulk-operation cases (the bulk journey
and the remaining-quota preview at two viewports), plus the eight `AXR1-021`
palette and search cases (the palette journeys at two viewports and the
long-name reflow checks at 390 and 320 px), plus the six `AXR1-022`
cases (the owner's profile, preference and account-export journey, staff not
seeing owner billing or profile controls, and the two header-width checks), plus the two `AXR1-027`
Automações cases (pause, resume and dead-letter retry/cancel at desktop and mobile). Every test in
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

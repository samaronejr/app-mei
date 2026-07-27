# Draft — accounting-mei-saas

```yaml
slug: accounting-mei-saas
intent: clear
review_required: true
classify: architecture
status: plan-written
pending_action: none — awaiting user choice (start work vs high-accuracy review)
```

## Request

Build a multi-tenant SaaS for accounting firms + paired MEI self-service portal.
Anchor document: `solo-build-plan-accounting-mei-saas.md` (strategy, already decision-complete).
Basis document: `deep-research-report.md` (market, compliance, ER model, RBAC matrix).

## Routing rationale

CLEAR. The user supplied a strategy document that already decides stack, tenancy model,
module boundaries, MVP cut, integration sequencing, and a 5-phase roadmap with hour
estimates. The outcome is not fuzzy. Remaining unknowns are preferences/tradeoffs the
repo cannot answer → interview, do not default-and-silence.
No review modifier said → `review_required: false`; offer high-accuracy review at delivery.

## Ledger — verified facts (with source)

### Repo state
- `/home/samarone/Documents/app_mei` is GREENFIELD: zero source files. Only the two
  markdown docs, `.omo/run-continuation/` (5 idle session markers), and a `.codegraph/`
  symlink whose index contains no code symbols.
- NOT a git repository. No `.git/`.
- **PRIOR ART FOUND**: `/home/samarone/Documents/clinic_project/` — real Django project,
  5 commits, clean worktree, main branch.
  - `pyproject.toml`: `django==5.2.*`, Python `>=3.12,<3.13`, psycopg[binary]>=3.2,
    celery, redis, DRF, django-otp==1.7.*, sentry-sdk[django], gunicorn, django-environ.
  - dev group: mypy (`strict = true`) + django-stubs + drf-stubs, ruff (`select = ["ALL"]`,
    line-length 88), pytest + pytest-django + pytest-cov (`filterwarnings = ["error"]`),
    model-bakery, pre-commit. Package manager: uv.
  - Layout: `config/settings/{base,dev,prod,test}.py`, `apps/core/`, `templates/`,
    `tests/`, `docker-compose.yml`, `Makefile`, `.env.example`, `.pre-commit-config.yaml`.
  - Commits include `chore(config): compose postgres 16, settings, idempotent role bootstrap`
    and `fix(db): allow owner resolver role ownership transfers` → PostgreSQL role
    bootstrapping already solved, directly relevant to the RLS app-role/migration-role split.
  - DIVERGENCE: clinic uses a fixed `search_path=clinic_app,public` (schema namespacing).
    app_mei requires shared-schema + `tenant_id` + RLS. Different model — do not assume reuse.
  - MISSING vs app_mei needs: no allauth, no HTMX/Alpine/Tailwind, no RLS policies,
    no tenant middleware/TenantScopedManager, no object storage, no PSP.

### Django / stack currency (PRIMARY SOURCE: djangoproject.com/download, fetched 2026-07-27)
- Django **5.2 LTS** — latest 5.2.16; mainstream ended 2025-12-03; **extended support to April 2028**.
- Django **6.0** — latest 6.0.7; mainstream to Aug 2026; **extended support ends April 2027**.
- Django **6.1** — releases Aug 2026 (6.1rc1 out now); extended to Dec 2027.
- Django **6.2 LTS** — releases **April 2027**; extended support to **April 2030**.
- ⚠️ A sub-agent recommended abandoning 5.2 LTS for 6.0. **REJECTED — that is backwards.**
  6.0 loses support April 2027, a full year BEFORE 5.2 LTS (April 2028). For a solo,
  multi-year compliance product, 5.2 LTS is correct. Anchor doc's "LTS into 2028" CONFIRMED.
- DECISION: pin Django 5.2 LTS. Plan the 6.2 LTS migration for after April 2027.

### Auth stack
- CLAIM (medium confidence, sub-agent): `django-allauth` ships built-in `allauth.mfa`
  (TOTP + recovery codes + WebAuthn/passkeys) via `pip install "django-allauth[mfa]"`,
  making a separate `django-otp` redundant.
- Anchor doc proposes allauth + django-otp; clinic_project uses django-otp (no allauth).
- DECISION (default, reversible internal): use `django-allauth[mfa]`, drop django-otp.
  Diverges from clinic_project deliberately. → executor must VERIFY allauth.mfa TOTP
  support against current allauth docs before implementing; fall back to django-otp if false.

### Brazilian fiscal parameters (as of 2026-07-27)
- **MEI ceiling R$ 81.000 for 2026 — CONFIRMED, unchanged.** Proportional opening-year
  rule (~R$ 6.750 x months) CONFIRMED for 2026.
- **LIVE LEGISLATIVE RISK**: increases are actively in the legislature (PLP 108/2021 —
  Senate-approved 2021, still in Chamber special commission as of Apr 2026, NOT enacted;
  plus a newer government proposal for R$ 110.000 in 2027 / R$ 140.000 in 2028 with a
  2-employee limit). NEITHER IS LAW. → This VALIDATES the anchor doc's §6 decision that
  the obligation engine must be parameter-data, not code. Ceiling must be a versioned,
  effective-dated row, never a constant.
- **DAS due day 20** of following month — CONFIRMED.
- **CNPJ alfanumérico**: IN RFB nº 2.229/2024 (effective 2024-10-25); production for NEW
  registrations **2026-07-31 — four days from now**. 14 positions, 0-9 + A-Z, check digit
  módulo 11 over ASCII−48. Old numeric CNPJs coexist unchanged. → Hard day-one requirement.
  Exact algorithm NOT yet extracted from the RFB PDF (agent blocked on PDF parsing) →
  carried as a plan todo with property tests, not assumed.
- **NFS-e Nacional**: mandatory for MEI since 2023-09-01. ME/EPP become mandatory
  2026-09-01 (Resolução CGSN 189/2026). MEI can issue free via Emissor Nacional web/app
  with gov.br login and NO certificate; **the REST API requires an e-CNPJ certificate**.
  → CONFIRMS the anchor doc's portal-assist-first decision. Native API path is correctly deferred.
- **MEI employee limit: 1** for 2026 (2 only under unenacted bills).
- ⚠️ DISCREPANCY TO RESOLVE: anchor doc says DASN-SIMEI is due "last **business** day of May";
  research returned "31 May". Materially affects the due-date engine. → plan todo must
  verify against Portal do Simples Nacional before encoding.

### RLS tenant isolation (highest technical risk) — research verified
- Use `set_config('app.tenant_id', %s, true)` (== `SET LOCAL`), NEVER plain `SET`.
- **`ALTER TABLE ... FORCE ROW LEVEL SECURITY` is mandatory** — table owners bypass RLS
  by default, so without FORCE the policy silently does nothing for the app if it owns tables.
- Fail-closed requires `current_setting('app.tenant_id', true)` (missing_ok) AND
  `NULLIF(..., '')` — a recycled pooled connection leaves the GUC as `''`, and `''::uuid`
  raises 22P02. Pattern: `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`.
- Pooling safety: `SET LOCAL` is SAFE with Django CONN_MAX_AGE>0, PgBouncer transaction
  mode, and PgBouncer session mode. Plain `SET` is UNSAFE with CONN_MAX_AGE>0 and with
  PgBouncer transaction mode (`server_reset_query_always=0` by default → no DISCARD ALL
  between transactions → cross-tenant leak).
- **Testing trap**: Django `TestCase` may run as superuser/owner and wraps in a transaction,
  making RLS tests pass VACUOUSLY. Use `TransactionTestCase` + connect as the non-superuser
  app role. Must also assert the no-context case returns ZERO rows.
- Add a meta-test over `pg_class.rowsecurity` / `pg_policies` asserting EVERY tenant-scoped
  table has RLS enabled + at least one policy — catches the "forgot a policy on a new table" bug.
- Index strategy: composite indexes with `tenant_id` as LEADING column.
- Libraries: `django-multitenant` (Citus) is app-layer filtering only, does NOT manage RLS.
  `django-rls-tenants` is low-confidence/emerging (single dev.to source). → roll our own.

## Adopted defaults (announced, not asked)

| Decision | Value | Why |
|---|---|---|
| Django | 5.2 LTS (→ 6.2 LTS after Apr 2027) | Primary source: support to Apr 2028 vs 6.0's Apr 2027 |
| Python | 3.13 | Supported by Django 5.2; longer runway than 3.12 |
| MFA | `django-allauth[mfa]`, drop django-otp | Avoids redundant dep; executor must verify first |
| RLS pattern | `set_config(...,true)` + FORCE RLS + NULLIF fail-closed | Verified research; fail-closed is non-negotiable |
| RLS library | roll our own | No mature RLS-aware Django package |
| Obligation params | versioned, effective-dated rows | Ceiling actively in legislature |
| Ceiling 2026 | R$ 81.000 | Confirmed current |

## RESOLVED forks (user answered 2026-07-27 — this constituted approval)

1. **Plan scope** → **Phases 0–1 (130–190h)**. Phases 2–4 out of scope, enumerated as a
   Must-NOT-Have table that final-wave audit F4 enforces.
2. **clinic_project reuse** → **Reference only — build fresh** (user chose this over my
   copy-conventions recommendation). Plan writes every config from scratch; clinic_project
   is cited in References as consultable prior art only, never copied.
3. **Deploy target** → **VPS (Hetzner-class) + Docker Compose**, Postgres local, no PgBouncer.
4. **Test strategy** → **TDD for fiscal/isolation core, tests-after for CRUD/templates/admin.**
   Agent-executed QA (happy + failure + evidence path) on every todo regardless.

## Plan artifact

`.omo/plans/accounting-mei-saas.md` — 47 todos across 8 waves + a 4-check final verification
wave. Template order intact, TL;DR leads the file.

## Metis gap analysis — receipts

Session `ses_05ae7cd91ffeaC6NBA18FLFmso` (bg_19eeca4c), completed in 10m15s. Adversarial
review of the pre-finalization approach. **Findings folded in; several were genuine blockers:**

| Finding | Severity | Resolution |
|---|---|---|
| `ATOMIC_REQUESTS` wraps only the VIEW — middleware `set_config` lands in its own transaction and is discarded before the view runs | BLOCKER | T-011 rewritten: `TenantMiddleware` owns an explicit `transaction.atomic()` spanning `get_response` **and** `response.render()`. `ATOMIC_REQUESTS` flipped to False in T-004. |
| Template rendering happens outside the transaction → lazy querysets evaluate with no tenant context → silently empty pages | BLOCKER | Same fix; plus a dedicated regression test rendering a `TemplateResponse` holding a lazy queryset. |
| `Membership` behind an `app.tenant_id` policy = bootstrap circularity; middleware must read it BEFORE tenant is known → nobody can ever log in | BLOCKER | T-008: `Tenant`/`Membership`/`Invite` moved to `NON_TENANT_TABLES`, protected app-layer by `request.user`; explicit test that Membership reads with no tenant context. |
| `RunPython` data migrations run as table owner under FORCE RLS → see ZERO rows → backfills silently no-op | BLOCKER | T-009: `app_migrator` granted `BYPASSRLS`; `app_runtime` never. |
| pytest-django needs CREATEDB; `TransactionTestCase` TRUNCATEs (collides with audit's revoked DELETE) — non-superuser app role cannot run the suite | BLOCKER | T-009: third role `app_test` owns the test DB, then `SET ROLE app_runtime` for assertions. |
| Meta-test asserting "≥1 policy exists" passes for a `FOR SELECT`-only or `USING(true)` policy | MAJOR | T-014: assert `cmd='ALL'`, `with_check IS NOT NULL`, neither expression literal `true`, `tenant_id NOT NULL`, plus an inverse allow-list check. |
| Anchor doc contradicts this plan in 6 places (Python 3.12, django-otp, 8 Phase 2–4 apps marked "✅ in MVP", backups to second provider, fail-loud RLS form, `SET LOCAL` with bind params) | MAJOR | Explicit "Document precedence" table added to Scope: this plan supersedes the anchor. |
| MEI ceiling modelled as a single scalar — MEI-Caminhoneiro has its own (~R$251.600) | MAJOR | T-036: `mei_category` added to `FiscalParameter` and `ClientCompany`; resolver category-aware. |
| Desenquadramento 20% tolerance band absent — ≤20% vs >20% differ by a full year of retroactivity | MAJOR | T-042: four bands incl. `exceeded_within_tolerance` / `exceeded_over_tolerance`, each reporting a computed effective date; tolerance itself a parameter row; verify-first against CGSN. |
| No dead-man's switch — if `beat` is down nothing raises, no calendar generates, Sentry sees nothing | MAJOR | T-041: `SchedulerHeartbeat` + `/healthz` reports unhealthy on stale heartbeat. |
| `task_acks_late` makes double execution expected, but idempotency was only a convention | MAJOR | T-041: `UniqueConstraint(tenant, client, obligation_type, competence_month)`. |
| `.evidence/` is git-ignored but F1 audits file existence → vacuous failure on clean clone | MAJOR | F1 now REGENERATES evidence by re-running acceptance commands. |
| LGPD baseline (rate limiting, DPO, incident runbook, DSR intake) called non-negotiable but absent from all waves | MAJOR | Folded into T-019, plus a PII-shape test forbidding CPF/CNPJ values in append-only `Event.metadata`. |
| Tenant resolution (subdomain vs session) undecided but cascades into ALLOWED_HOSTS/CSRF/cookie/TLS written in W1 | MAJOR | Decided: **subdomain**, recorded in Decided-constraints and T-004. |
| Claim "no PgBouncer removes the top RLS leak vector" overstated — Django `CONN_MAX_AGE` carries the same risk | MINOR | Wording corrected in Decided constraints. |

**Known accepted risk, surfaced not silenced**: Metis argues 47 todos in 130–190h (~3–4h each
including two QA artifacts and a commit) is optimistic, particularly Wave 2's 8 RLS todos.
Recorded in the TL;DR effort note rather than papered over.

## High-accuracy review

**REQUESTED by the user 2026-07-27** → `review_required: true`. Round 1 dispatched together
against the complete plan file (todos + TL;DR filled):

### Round 1
| Pass | Agent | Background id | Session | Verdict |
|---|---|---|---|---|
| 1 | native `momus` | `bg_c602ea4f` | `ses_05ad8e4aaffez22Rvg0ziX2WHv` | **REJECT** — 11 issues |
| 2 | independent `oracle` | `bg_e0192c22` | `ses_05ad87f55ffev1iNLKCDESCIsQ` | **REJECT** — 16 issues |

Round-1 headline defects: T-027's RBAC criterion arithmetically impossible (96 cells not 102; 4-value enum
could not encode 7 matrix values); `NON_TENANT_TABLES` under-populated → CI red in 4 waves; T-028's guard
test rejected code T-017 mandates; **T-007 manager wiring inverted** (declaring `all_objects` first made the
UNSCOPED manager `_default_manager`); **`ATOMIC_REQUESTS=False` silently lost rollback-on-exception**
(`convert_exception_to_response` turns a view exception into a 500 INSIDE the atomic block → partial writes
COMMIT); isolation suite would have died on `permission denied` (app_test owns test tables, so app_migrator's
default privileges never applied). **Oracle also RESOLVED both open fiscal rules**: DAS rolls FORWARD
(CGSN 140/2018 art. 40 §3 — anchor was right, research wrong); DASN is fixed 31 May with NO rolling (art. 109
— anchor was wrong). All 27 folded in.

### Round 2 (resubmitted fresh against the revised plan)
| Pass | Agent | Background id | Session | Verdict |
|---|---|---|---|---|
| 1 | native `momus` | `bg_c791ceb8` | `ses_05ac77653ffeWC5I6CVZCa2k7E` | **REJECT** — 9 issues |
| 2 | independent `oracle` | `bg_eaab7278` | `ses_05ac71930ffejrBe7D6eBaopps` | **REJECT** — ~18 issues |

Round 2 confirmed the round-1 fixes landed, but found that several **introduced new defects** — the expected
hazard of heavy editing, and the reason resubmission is mandatory rather than optional.

**Fixed in this pass:**
- TL;DR still advertised 4 verify-first gates incl. the two now RESOLVED → executor would have re-litigated settled fiscal rules.
- T-002a evidence paths + T-016 reference still said `T-015` → F1 would fail a correctly-executed todo.
- T-032 still demanded "≥3 official vectors" that T-031 had just established do not exist.
- **T-032's property test was mathematically FALSE**: mod-11 over `ord−48` has collisions (`A`=17, `L`=28, `W`=39 all ≡6), so `12.LBC.345/01DE-35` is a VALID mutation of the official vector. Hypothesis would have found it and failed. Replaced with the residue-aware property + a documented collision regression test.
- **T-036's `ExclusionConstraint` would have rejected the very insert three todos require** — an open-ended `valid_to=NULL` row overlaps any future row, destroying the "change the 2027 ceiling with no deploy" property; it was also inert for the three `mei_category IS NULL` rows. Replaced with latest-wins effective dating, which also removes `btree_gist`, `django.contrib.postgres`, and a `CREATE`-on-database grant `app_migrator` lacks.
- `NON_TENANT_TABLES` mixes globs into a set consumed by exact membership → T-014 red on every django/auth/allauth table. Now specified as `fnmatch`.
- T-013's `tenant_context` had the same savepoint/GUC-merge bug T-011 fixed: on `RELEASE SAVEPOINT` the value persists to the parent, so a `PlatformTask` loop leaks the last tenant. Now requires explicit save/restore.
- **T-020's admin was broken by the T-007 fix**: on the platform hostname there is no tenant, so the scoped `_default_manager` AND RLS both return zero rows — every changelist empty, acceptance passing vacuously. Resolved with an explicit tenant selector.
- T-019c's `DataSubjectRequest`, T-026's implicit Tag M2M join table (no tenant column → cross-tenant write vector), and T-029's template table all lacked allow-list entries.
- T-037 QA still counted "four" parameters against a seven-row table.

**KNOWN REMAINING — round 3 required before this can be called review-passed:**
T-011's streaming decision still defers to T-033 (4 waves later, and T-033 never states it) and its rollback
trigger covers only `>=500`, missing `Http404`/`PermissionDenied` which `ATOMIC_REQUESTS=True` would have rolled
back; Wave-2 order is still circular (needs T-007 split into T-007a/T-007b around T-010/T-012); three decisions
are still delegated to the executor with dead-end cross-references (uuid7 worker pinning → T-022 silent;
composite FK → T-026/T-041/T-042 silent; `platform_admin` → T-008 silent); T-042 is an undeclared third gate
firing after T-037 seeds the value it governs; T-018's REVOKE is re-granted by T-009's blanket fixture GRANT;
`app_test` lacks BYPASSRLS so data migrations diverge prod/test; no custom user model decision despite
email-only allauth; AccessLog middleware ordering would roll away the Marco Civil record on a 500.

### Round 3
| Pass | Agent | Background id | Session | Verdict |
|---|---|---|---|---|
| 1 | native `momus` | `bg_09c3fea2` | `ses_05ab6b2c8ffeqeFsv5EU9dIExF` | **REJECT** — 3 issues |
| 2 | independent `oracle` | `bg_91f71812` | `ses_05ab661afffejL2LrBvyD2rRr2` | **REJECT** — ~10 issues |

Momus trajectory: 11 → 9 → **3**, and all three of round 3's were one class — a fix applied to a todo's
body without propagating to its acceptance criteria or its dependent todo.

**Two genuinely new deep findings from Oracle, both verified against Django 5.2.16 source and live PostgreSQL 16:**
- **`ATOMIC_REQUESTS` is NOT a module-level Django setting** — it appears zero times in `global_settings.py`
  and is exclusively a per-database key (`db/utils.py` does `conn.setdefault("ATOMIC_REQUESTS", False)`).
  But `Settings.__init__` copies every uppercase module attribute, so `ATOMIC_REQUESTS = True` in `base.py`
  makes `settings.ATOMIC_REQUESTS` return `True` **while the connection's value stays `False`**. The view is
  never wrapped, no savepoint exists, every `Http404`/`PermissionDenied` commits partial writes — and the
  plan's own acceptance criterion **certified the broken system as fixed**. Now set in the DATABASES dict,
  with every assertion reading `connections["default"].settings_dict["ATOMIC_REQUESTS"]`.
- **The round-2 `app_test` BYPASSRLS grant defeated the vacuous-pass guard.** Reproduced on PG16: owner +
  `BYPASSRLS` + `rolsuper = false` sees ALL rows under `FORCE ROW LEVEL SECURITY` with no GUC — while a
  "not superuser" guard passes. Worse, `SET ROLE` is dropped two ways: `TransactionTestCase._post_teardown`
  closes connections after every test, and `SET ROLE` inside a transaction is reverted by `ROLLBACK`. Fixed
  with a function-scoped autouse fixture plus a per-test assertion of `current_user`, `rolsuper`,
  `rolbypassrls`, and two new QA mutations. Also: denial cases must run through `all_objects`/raw cursor,
  since `Model.objects` would be satisfied by the ORM filter alone with RLS switched off.

Other round-3 fixes: T-036's acceptance still demanded the constraint round 2 removed (+ `nulls_distinct=False`
needed, since PG treats NULLs as distinct and three seeded rows carry `mei_category = NULL`); T-011's Do list
still ordered `ATOMIC_REQUESTS = False`; T-009 contradicted itself on the test role (one branch could not
`CREATE DATABASE`); `accounts_*` from the custom user model was missing from `NON_TENANT_TABLES`; T-032's
documented collision classes were factually wrong (`{0,B,M,X}` → `{7,B,M,X}`, `{A,L,W}` → `{6,A,L,W}`, and
they span digits as well as letters); T-042 asserted only 2 of 4 legislated desenquadramento outcomes (the
opening-year >20% branch resolves to `opened_on`, not 1 January); T-020's tenant selector addressed only the
RLS layer, not the ContextVar; T-012's acceptance referenced `Model.objects`, which the T-007 split moved to
T-007b at a later position; T-014 lacked the `fnmatch` instruction T-010 required it to carry.

**ARCHITECTURE EMPIRICALLY VALIDATED.** Oracle ran live PostgreSQL 16 and confirmed the load-bearing premises:
GUC set on an outer transaction survives savepoint rollback (so `ATOMIC_REQUESTS` nesting works as designed);
`SET LOCAL` merges into the parent on `RELEASE SAVEPOINT` (so T-013's explicit save/restore is genuinely
required); `SET ROLE` causes RLS to apply as the target role; composite FK blocks cross-tenant references
while a single-column FK does not. The design is sound — every defect across three rounds was in the writing.

### Round 4 — six lenses, not two
| Lens | Agent | bg id | Verdict |
|---|---|---|---|
| Plan quality | `momus` (+tdd, +verify-before-complete) | `bg_ec45596f` | **REJECT** — 2 |
| Technical | `oracle` | `bg_94a6397c` | **REJECT** — ~8, empirically verified on live PG16 + Django 5.2.16 |
| **Adversarial security** | `oracle` (+security-review) | `bg_b0eab7a5` | **13 ranked attacks** |
| Mechanical consistency | `explore` | `bg_d66bf18b` | — |
| Intra-todo contradictions | `explore` | `bg_424cacfa` | — |
| Claim verification | `librarian` | `bg_56f86af5` | — |

**Two total blockers that would have stopped Wave 2 dead:**
- `ops/sql/roles.sql` never issued `GRANT app_runtime TO app_test`. PostgreSQL requires role membership for
  `SET ROLE`. Verified empirically: `permission denied to set role "app_runtime"`. **The entire isolation
  suite — the centrepiece of this plan — died on its first statement.** One missing line.
- `audit_event` is tenant-scoped with `WITH CHECK`, but at **login** the GUC is still empty, so the INSERT is
  rejected. T-018's own criterion ("logging in produces exactly one event") was **unachievable**. Same killed
  anonymous invite acceptance and DSR intake. Resolved by splitting `Event` (tenant-scoped) from
  `PlatformEvent` (platform-level, for pre-tenant identity events).

**Adversarial pass — the RLS core HELD.** 8 direct attacks on the row layer, all blocked by controls already
specified. But every serious attack goes **around** it:
- **CRITICAL — admin tenant selector.** T-020 said which tenant to select, never *which tenants a user may*
  select. `Tenant` is outside RLS, and the choice arrives as a request parameter → IDOR. Invisible to both
  layers *by design*: GUC says tenant B, RLS correctly returns B's rows. Nothing is violated. One `is_staff`
  account reads every firm. T-020's criterion passed vacuously (select your own tenant, assert rows are yours).
- **CRITICAL — parent-domain session cookie.** Django's docs state the precondition verbatim: *"Django session
  security requires that sites are deployed such that untrusted users do not have access to any subdomains."*
  This design gave one subdomain per mutually-untrusted firm. Cookie tossing → session fixation → the victim
  silently works inside the attacker's tenant while both layers approve and the audit log blames the victim.
  **This was my own earlier decision.** Corrected to host-only cookies + `__Host-` prefix.
- **HIGH — login rate limit keyed `(tenant_id, ip)`** while credentials are global → 5×N attempts/min by
  rotating the Host header. The old acceptance criterion *literally asserted the bypass as a feature*.

All round-4 findings fixed in this pass. Verified mechanically (fresh output): 50 headings, 3 count-claims
agreeing, zero stale cross-references, 8 template headers in order, all 6 critical fixes present.

**Status after round 4: high-accuracy review NOT PASSED** (both formal passes REJECT). Fixes applied but not
re-verified by a fresh round. Totals: **~80 defects found and fixed across 4 rounds.** Trajectory (momus):
11 → 9 → 3 → 2. The residual class is edit-propagation, not design.
Notepad: `/tmp/ulw-20260727-180332-accounting-mei-review4.md`

Rules being followed: exactly ONE momus + ONE independent review per round, dispatched
together. Keep Momus in flight — elapsed time alone never justifies cancelling, duplicating,
or treating it as failed. After both verdicts return, fix EVERY cited issue and resubmit BOTH
fresh. Do not report "high-accuracy review completed" unless both receipts exist and both
final verdicts are unconditional approval.

## Test strategy (to confirm at gate)

TDD (test-first) for the fiscal/isolation core: CNPJ/CPF alphanumeric validators,
due-date engine, R$81k threshold monitor, RBAC `can()` matrix, cross-tenant RLS denial.
Tests-after for CRUD/templates/admin. Both mandated regression packs (CNPJ alfanumérico,
municipality variance) exist from day one. Agent-executed QA on every todo regardless.

## Gate

Brief presented. Awaiting explicit approval to write `.omo/plans/accounting-mei-saas.md`.
Approval authorizes writing the plan ONLY — never implementation.
On resume after compaction: read this file, do NOT re-explore, resume at the gate.

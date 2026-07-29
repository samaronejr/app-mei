# Phase 2a — Portal isolation foundation - Work Plan

> **Revision 7.** Six adversarial review rounds; 34 blocking findings, all accepted and
> fixed, none argued down. Oracle reproduced six of them on a live PostgreSQL 16.14 cluster.
> Every fix is traceable to a finding — receipts in `.omo/drafts/phase-2a-portal-isolation.md`.
>
> **Wave-1 status (round 6): T-051…T-055 and T-057 are APPROVED for implementation.**
> T-056 was patched in this revision (W1-BL-1, W1-BL-2) and must be re-reviewed before the
> meta-test is written. Wave 3 (T-061/T-062) remains under review.

## TL;DR (For humans)

Phase 2 opens a portal where MEI business owners log in to see their own data. Phase 1's
isolation model cannot carry that weight unchanged: **RLS discriminates only on
`tenant_id`**, so a portal user placed inside the firm's tenant can read *every* client in
that firm at the database layer. The sole barrier between two competing MEIs would be one
application-layer `can()` check.

This plan builds the isolation layer and **nothing else**. It ends with a portal login page
that displays one client's name — deliberately unimpressive, deliberately full-stack.
Document upload, notifications and the PWA are Phase 2b, and must not start until the
coverage meta-test in **T-056** is green.

**The mechanism**: an `app_portal` database role entered with `SET LOCAL ROLE` inside the
existing tenant transaction, plus `RESTRICTIVE` policies that AND with the current
permissive tenant policies.

> **The single most dangerous fact in this plan.** PostgreSQL matches RLS `TO <role>`
> clauses by **privilege inheritance, not identity**. A default `GRANT app_portal TO
> app_runtime` therefore makes the restrictive portal policies apply to `app_runtime` as
> well — and the accounting firm sees **zero rows in every client table**. Reproduced on
> PostgreSQL 16.14. The grant **must** carry `WITH INHERIT FALSE, SET TRUE`, and T-056
> asserts it. `NOINHERIT` on the role itself does not help; the flag that matters is on the
> grant edge.

**Effort**: **7–10 days**, solo. (Revision 1 said 4–6, revision 2 said 6–9; round-2 review
added the grant allow-list, two more call sites, and the host-parsing work.) **Do not interleave with feature work.**

---

## Verify-first gates (block their waves — resolve BEFORE writing code)

Both of these were discovered in Wave 4 of revision 1, "which is where plans go to fail".

### V1 — How does `app_portal` come to exist on an already-initialised cluster? (blocks Wave 1)

`docker-compose.yml:44-46`: the postgres init hook runs **only against an empty data
directory**. Staging is already initialised. The migration route is **impossible**:
`ops/sql/roles.sql` sets `ALTER ROLE app_migrator ... NOCREATEROLE`, so migrations cannot
`CREATE ROLE`, and the grant needs ADMIN OPTION or superuser.

**Resolution (decided, not open)**: a documented `psql` step run as the bootstrap superuser
— exactly what CI already does (`.github/workflows/ci.yml:99`) — plus a new **`core.E008`** (`apps/core/checks.py` currently ends at E007)
startup check that fails loudly when `app_portal` is missing **or** when
`pg_has_role('app_runtime','app_portal','USAGE')` is true. Local dev requires
`docker compose down -v`. **Exit criterion**: the check exists, is registered, and fails on
a cluster without the role.

### V2 — Portal host scheme and TLS (blocks Wave 3)

`portal.<slug>.<domain>` is **four labels and cannot get a production certificate**. A public
wildcard covers exactly one label, so `*.example.com` does not cover
`portal.acme.example.com`, and no public CA issues `*.*.example.com`. The Caddyfile has a
single `{$SITE_ADDRESS}` block (`ops/Caddyfile:38`).

**Resolution (decided)**: use **`<slug>-portal.<domain>`**. Two labels, covered by the
existing wildcard, and still a distinct host so `__Host-` cookies remain separate
(`config/settings/prod.py:26-27`).

> **`slug_from_host` is NOT reusable as-is.** It returns `labels[0]` — for
> `acme-portal.example.com` that is the literal string `"acme-portal"`, and
> `Tenant.objects.filter(slug="acme-portal")` finds nothing. The 2-label *shape* needs no
> extension, but the returned value must have the suffix **stripped**. T-061 specifies
> `portal_slug_from_host()`, which requires the `-portal` suffix and returns `None` without
> it. `AdminTenantMiddleware:56-60` also consults `slug_from_host` and must be unaffected.

> **Slug collision.** `Tenant.slug` is `SlugField(unique=True)` with no reserved-name
> validation. A firm registering the slug `acme-portal` would take over `acme`'s portal
> host — and conversely T-062 would route that firm's own front door to the portal urlconf.
> Add a validator rejecting `*-portal`, plus a check over existing slugs. The same
> validator must cap `slug` at **56 characters** (`max_length=63` today) so that
> `<slug>-portal` remains a valid ≤ 63-character DNS label.

**Exit criteria**: the required `SITE_ADDRESS` shape is written down — `ops/Caddyfile:38` is a single `{$SITE_ADDRESS}` whose value is required-but-unspecified (`docker-compose.prod.yml:274`) and `TLS_DIRECTIVE` defaults to `tls internal`, so the wildcard is **assumed, not verified**; `acme-portal.<domain>` resolves, serves TLS, and is **accepted by
`ALLOWED_HOSTS`** (a `DisallowedHost` 400 is not covered by "resolves and serves TLS"), and
the reserved-slug validator is in place — all before T-061 begins.

---

## Scope

### What this plan covers

1. The `app_portal` role, its **SELECT-only** privileges, and the non-inheriting grant.
2. `RESTRICTIVE` policies on the 5 client-scoped tables, keyed on a new `app.client_id` GUC.
3. Portal decisions for the tables that carry RLS; an explicit exempt-list for those that do not.
4. A coverage meta-test that fails when a table lacks a portal decision, when RLS is off, or when the grant inherits.
5. A falsifiable client-to-client denial suite.
6. `current_client_id` ContextVar + `client_context()`.
7. `Membership` extended with a nullable composite client FK — **and the four firm-side call-site fixes it forces**.
8. `PortalMiddleware` on `<slug>-portal.<domain>`.
9. One portal login page proving the stack end to end.

### Explicitly OUT of scope (Must-NOT-Have)

| Item | Why | Phase |
| --- | --- | --- |
| Document vault, file upload, object storage | Requires this isolation layer first | 2b |
| **Portal write access of any kind** | Grants are SELECT-only in 2a; see Decided constraint 8 | 2b |
| Web push, service worker, manifest, PWA | iOS reach ≈1–2%; see Decided constraint 4 | deferred |
| WhatsApp Business API | Costs money, needs phone numbers we do not collect, pilots unasked | 3+ |
| Email templates, reminder sweeps | Belongs with the vault | 2b |
| Any portal feature beyond "log in, see your own company" | The tracer bullet stays thin | 2b |
| Enabling RLS on `audit_accesslog` / `tenants_membership` / `tenants_invite` / `audit_platformevent` / `audit_datasubjectrequest` | Reopens the Phase-1 bootstrap problem; needs its own plan | separate |
| Changing any existing RLS **policy** | Restrictive policies compose | never |

### Decided constraints (do not relitigate)

1. **Isolation is enforced by DB role, not by a shared GUC.** A single `app.client_id` GUC
   with `(guc = '' OR client_id = ...)` is **fail-open**. The fail-closed default must
   *differ by caller type* — firm staff see the whole tenant when no client is set; portal
   users must see nothing. Only the caller's role expresses that.

2. **The grant must not inherit.** `GRANT app_portal TO app_runtime WITH INHERIT FALSE, SET TRUE`.
   See the TL;DR warning. This is the difference between a working product and a total outage.

3. **`SET LOCAL ROLE app_portal`, same connection, same transaction.** Not a second
   connection and not a database router: 954 MiB, 1 OCPU. `SET LOCAL` is transaction-scoped
   and safe under pgbouncer *transaction* pooling. **A session-mode pooler breaks this
   silently** — record the assumption in the middleware docstring.

4. **Notifications for Phase 2 are email-only.** Web push reaches ~1–2% of iOS users (Apple
   requires manual Add-to-Home-Screen, no `beforeinstallprompt`). WhatsApp is right
   eventually (~95% read rate) but costs R$0.03–0.05/message, needs phone numbers we do not
   collect, and anchor §13 Q2 asks pilots that exact question. Write the notification layer
   channel-agnostically. **This consciously overrides the parent plan's Phase-2 line.**

5. **`client_id` is derived from the authenticated `Membership` row.** Never from URL, path,
   query, header, cookie or session. Enforced by grep guard **and** a behavioural test.

6. **PostgreSQL is 16.14** (verified on the deployed cluster): `nulls_distinct` and
   `GRANT ... WITH INHERIT FALSE` both require ≥16. `RESTRICTIVE` policies need ≥10.

7. **Threat model, stated plainly.** `SET LOCAL ROLE` is **not** a boundary against anything
   that can issue arbitrary SQL — `RESET ROLE` mid-transaction returns to `app_runtime` and
   reads everything (verified). This layer defends against **forgotten ORM filters and
   application-logic bugs**, which is the realistic portal failure mode. It does **not**
   defend against SQL injection or a rogue raw cursor; those remain the application's job.

8. **Portal is read-only in 2a.** Policies are `FOR ALL` (so 2b need not revisit them) but
   **privileges are `SELECT` only**. Policies and grants are separate decisions; revision 1
   conflated them, which would have let a portal user `DELETE` their own DAS obligations or
   rewrite `MonthlyRevenue`.

9. **Phase 1 invariants are untouchable**, with exactly one sanctioned exception recorded in
   S9 below.

### Document precedence

1. **This file** for Phase 2a. 2. `.omo/plans/accounting-mei-saas.md` for Phase 0–1
decisions. 3. `solo-build-plan-accounting-mei-saas.md` (anchor). 4. `deep-research-report.md`.

Conflicts on Phase-2 notification scope: **this file wins** (constraint 4). Any other
conflict: **stop and ask** — it is a symptom of a mistake, not a decision.

---

## Verification strategy

### Standing commands

| Purpose | Command |
| --- | --- |
| Lint | `uv run ruff check . && uv run ruff format --check .` |
| Types | `uv run mypy .` |
| Tests | `uv run pytest` |
| Isolation only | `uv run pytest tests/isolation -q` |
| Missing migrations | `uv run python manage.py makemigrations --check --dry-run` |

### Security requirements (binding)

| # | Requirement | Verified by |
| --- | --- | --- |
| S1 | `app_portal` has no SUPERUSER, no BYPASSRLS, no LOGIN, owns no table | **T-051** |
| S2 | `pg_has_role('app_runtime','app_portal','USAGE')` is **false** and `pg_has_role('app_runtime','app_portal','SET')` is **true**. *`has_privs_of_role()` is a C internal and NOT SQL-callable — it errors at runtime. `'MEMBER'` is true in both cases and must never be used.* | **T-051**, **T-056** |
| S3 | `app_portal` holds **SELECT only** — no INSERT/UPDATE/DELETE anywhere | **T-051** |
| S4 | Every client-scoped table carries a restrictive `app_portal` policy naming `app.client_id` and the right column | **T-056** |
| S5 | Every table carrying a portal policy has `relrowsecurity` **and** `relforcerowsecurity` true | **T-056** |
| S6 | Every tenant-scoped table has an explicit portal decision (policy or justified exemption) | **T-056** |
| S7 | A portal session with no `app.client_id` reads **zero** rows | **T-057** |
| S8 | A portal session cannot read or write another client's rows in the same tenant | **T-057** |
| S9 | Phase-1's isolation assertions still hold. The filter below deliberately blinds `test_the_policy_shape_is_not_merely_present` **and** `test_the_tenancy_root_tables_are_deliberately_unpoliced` to *portal* policies; **T-056 assumes that coverage**, which is why its `with_check` assertions are mandatory. **One sanctioned edit**: `_policies()` at `tests/isolation/test_rls_coverage.py:68-75` gains a `roles = '{public}'` filter so Phase-1 shape assertions describe Phase-1 policies only. Re-baseline the count from 41 and record the new number. No other Phase-1 test may change. | every commit |
| S10 | No firm-side `Membership` query silently absorbs client rows | **T-059** grep guard + per-site tests |
| S11 | `client_id` cannot be influenced by any request-controlled value | **T-062** guard + behavioural test |

### The falsification requirement

Phase 1 earned trust because removing RLS turned tests red. **Every protection here must be
provably falsifiable**; T-064 removes each in turn. A protection whose removal changes
nothing is decoration and must be fixed or deleted.

---

## Execution strategy

| Wave | Contents | Gate |
| --- | --- | --- |
| — | **V1** role provisioning | before Wave 1 |
| 1 | Role, operations, policies, exempt-list, meta-test, denial suite (T-051…T-057) | — |
| 2 | ContextVar, Membership + 4 call-site fixes, `role_of` (T-058…T-060) | Wave 1 green |
| — | **V2** host/TLS scheme | before Wave 3 |
| 3 | PortalMiddleware, dispatch, login page (T-061…T-063) | Wave 2 green |
| 4 | Mutation matrix, staging deploy (T-064…T-065) | Waves 1–3 |

### Operating rules

1. Every commit leaves the tree green.
2. Every todo writes QA stdout to `.evidence/T-0XX-{happy,failure}.txt`.
3. No `# type: ignore`, no `cast(`, no `# pragma: no cover`.
4. Never modify a Phase-1 **policy**. The only sanctioned test edit is the one named in S9.
5. If a todo cannot be completed as written, **stop and report** — never improvise around a security control.
6. **Before any plan revision, run the propagation sweep**: for every mechanism the plan cites, grep every *other* place it now applies. And for every assertion specified as a substring/`in` test, construct the **nearest wrong artifact** and confirm the assertion rejects it. Six rounds of review show the dominant defect class is a correct insight applied in one place and not its sibling — including, twice, the fix for a propagation defect not itself being propagated.

---

## Todos

<!-- BEGIN TODOS -->

### Wave 1 — Portal isolation at the database layer

#### T-051 — The `app_portal` role, SELECT-only, non-inheriting

**References** — `ops/sql/roles.sql`; V1 above. Fixes **B1** and **A1**.

**Do** — Extend `ops/sql/roles.sql`, matching the existing idempotent `DO $$ ... EXCEPTION WHEN duplicate_object` pattern:

```sql
CREATE ROLE app_portal NOLOGIN NOINHERIT;
GRANT USAGE ON SCHEMA public TO app_portal;

-- ALLOW-LIST, not blanket. `GRANT SELECT ON ALL TABLES` would also expose
-- accounts_user (email + password hash), mfa_authenticator (TOTP secrets),
-- account_emailaddress, django_session and every cross-tenant audit row — none of
-- which T-056 inspects, because it iterates only tables carrying tenant_id. Naming
-- the six tables the portal actually needs makes every future table closed by
-- default, and makes a missing grant fail LOUDLY (permission denied) rather than
-- silently-empty.
-- Guarded with to_regclass, mirroring tests/conftest.py:60-68. roles.sql runs from the init
-- hook against an EMPTY data directory and the postgres entrypoint uses ON_ERROR_STOP=1 (as
-- does ci.yml:99), so a bare `GRANT ... ON <table>` for a table that does not exist yet
-- ABORTS cluster bootstrap. Verified: `ERROR: relation "clients_clientcompany" does not
-- exist`. That breaks `docker compose up` on a fresh volume — which V1 mandates — and every
-- CI run, before lint.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['clients_clientcompany','clients_clientassignment',
                           'clients_clienttag','clients_onboardingitem',
                           'obligations_obligation','obligations_monthlyrevenue']
  LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON public.%I TO app_portal', t);
    END IF;
  END LOOP;
END $$;

-- INHERIT FALSE is load-bearing: with the default (INHERIT TRUE) the RESTRICTIVE
-- policies below bind app_runtime too and the firm sees zero rows in every client
-- table. SET TRUE still permits `SET LOCAL ROLE app_portal`. PostgreSQL 16+.
GRANT app_portal TO app_runtime WITH INHERIT FALSE, SET TRUE;
```

**No `ALTER DEFAULT PRIVILEGES`** — new tables must be granted deliberately.

> **On a fresh cluster the guard above grants nothing — the tables do not exist yet.** The
> grant must therefore ALSO run after `migrate`, in all three environments: **dev/staging**
> via a documented post-`migrate` step (a management command, or the same `DO $$` block
> re-run through `psql` — fold it into V1's superuser step, **run as `app_migrator`**, which owns the dev tables; `app_runtime` cannot grant. Re-run it whenever the six-table allow-list grows, and update `README.md` / `ops/README.md`: `docker compose up -d --wait` alone no longer yields a working portal), and **test** via
> `django_db_setup` below. `roles.sql` alone is necessary but never sufficient.

> **The grant must also reach the test database, or every portal test dies before it
> reaches a policy.** `ops/sql/roles.sql` runs from the init hook against an *empty*
> cluster, so `ON ALL TABLES` forms grant nothing, and `ALTER DEFAULT PRIVILEGES` without
> `FOR ROLE` only covers tables created by the executing superuser — dev tables are owned
> by `app_migrator`, test tables by `app_test`. `tests/conftest.py:42-69` currently grants
> only `RUNTIME_ROLE`. **Extend `django_db_setup` with the same six-table grant for
> `app_portal`.** Without it T-053/T-054/T-057 all fail with `permission denied` before any
> policy is evaluated — and T-057 test 5 would pass for entirely the wrong reason.

**SELECT only** — no INSERT/UPDATE/DELETE (constraint 8). Also add the `core.E008` startup check from V1.

**Acceptance criteria** — `docker compose down -v && docker compose up -d --wait` **succeeds** (the guarded grant must not abort bootstrap). `has_table_privilege('app_portal', ...)` is true for all six tables **on the dev database after `migrate`**, not only in the test DB. `app_portal` exists: `rolsuper=false`, `rolbypassrls=false`, `rolcanlogin=false`, owns zero tables. `pg_has_role('app_runtime','app_portal','USAGE')` is **false**. `pg_has_role('app_runtime','app_portal','SET')` is **true**. `app_portal` holds **SELECT on exactly the six allow-listed tables and no privilege of any kind on any other table** — assert **both** directions, since a role with no grants at all makes the negative half pass vacuously. Re-running `roles.sql` is a no-op. The startup check fails on a cluster lacking the role.
**QA — happy**: role attributes, both `*_role` assertions, empty write-grant query, `SET ROLE` round-trip → `.evidence/T-051-happy.txt`
**QA — failure**: `GRANT app_portal TO app_runtime WITH INHERIT TRUE;` — a plain re-`GRANT` of an existing membership is a **verified no-op**, so the explicit `WITH INHERIT TRUE` is required or this test cannot fail. Show `pg_has_role(...,'USAGE')` flip to true and a firm-side SELECT return 0 rows; restore with `WITH INHERIT FALSE;` and show rows again → `.evidence/T-051-failure.txt`
**Commit**: `feat(ops): app_portal role, select-only and non-inheriting`

---

#### T-052 — Portal RLS migration operations

**References** — `apps/core/migrations/_operations.py::EnableRLS` (underscore prefix required, else `BadMigrationError`).

**Do** — Add to `_operations.py`:

- `EnablePortalClientRLS(model_name, tenant_field="client_id")` — **takes a model name and resolves `_meta.db_table`, exactly as `EnableRLS` does**. A raw table string loses `allow_migrate_model` and raises `LookupError` from `get_model()`. Match `EnableRLS`'s kwarg name (`tenant_field`), not `column`:
  ```sql
  CREATE POLICY {table}_portal_client_isolation ON {table}
    AS RESTRICTIVE FOR ALL TO app_portal
    USING      ({column} = NULLIF(current_setting('app.client_id', true), '')::uuid)
    WITH CHECK ({column} = NULLIF(current_setting('app.client_id', true), '')::uuid);
  ```
- `DenyPortalAccess(model_name)` — `AS RESTRICTIVE FOR ALL TO app_portal USING (false) WITH CHECK (false)`. Same signature rule.

Both need working `database_backwards`. **Neither may be applied to a table without RLS enabled** — raise if `relrowsecurity` is false, so B2 cannot recur silently. Two details: **skip the check when `schema_editor.collect_sql` is true**, or `sqlmigrate` / `migrate --plan` raise spuriously against a database where the table does not yet exist; and take a **`model_name`** as `EnableRLS` does (resolving `_meta.db_table`) rather than a raw table string, which would lose `allow_migrate_model`.

**Acceptance criteria** — Operations are importable and reversible. A unit test asserts generated SQL contains `AS RESTRICTIVE`, `TO app_portal`, `NULLIF(`, `, true)`, and `app.client_id`. Applying either to an RLS-disabled table raises.
**QA — happy**: apply then reverse each → `.evidence/T-052-happy.txt`
**QA — failure**: applying to an RLS-disabled table raises with a clear message → `.evidence/T-052-failure.txt`
**Commit**: `feat(core): portal RLS migration operations`

---

#### T-053 — Restrictive policies on the 5 client-scoped tables

**References** — Verified against the deployed cluster. Each already has a composite `(tenant_id, client_id)` FK, so `client_id` and `tenant_id` necessarily name the same firm.

**Do** — `EnablePortalClientRLS` on exactly these five, **passing model names** — `ClientAssignment`, `ClientTag`, `OnboardingItem`, `MonthlyRevenue`, `Obligation` — not the table names: `clients_clientassignment`, `clients_clienttag`, `clients_onboardingitem`, `obligations_monthlyrevenue`, `obligations_obligation`. All five already have RLS enabled.

**Acceptance criteria** — All 5 carry both the Phase-1 permissive policy and the restrictive portal policy (`pg_policies.permissive='RESTRICTIVE'`, `roles={app_portal}`). **Firm-side reads are unaffected** — assert row counts as `app_runtime` before and after.
**QA — happy**: `pg_policies` for all 5 + unchanged firm-side counts → `.evidence/T-053-happy.txt`
**QA — failure**: `uv run pytest tests/isolation -q` still green at the S9 baseline → `.evidence/T-053-failure.txt`
**Commit**: `feat(clients,obligations): restrictive portal policies on client-scoped tables`

---

#### T-054 — `ClientCompany` portal policy (special case)

**References** — `clients_clientcompany` has no `client_id`; its own `id` **is** the client identity. RLS is already enabled on it.

**Do** — `EnablePortalClientRLS("ClientCompany", tenant_field="id")` — the **model** name, not the table — yielding `USING (id = NULLIF(current_setting('app.client_id', true), '')::uuid)`.

**Acceptance criteria** — A portal session sees exactly its own row; zero with no `app.client_id`. Firm-side reads unchanged.
**QA — happy**: portal session selects exactly its own id → `.evidence/T-054-happy.txt`
**QA — failure**: portal session reading a sibling company by id gets 0 rows → `.evidence/T-054-failure.txt`
**Commit**: `feat(clients): portal sees only its own client company`

---

#### T-055 — Portal decisions for the remaining tables

**References** — Fixes **B2**, **B3**, **B4**. Revision 1 targeted 7 tables; **5 of them have RLS disabled**, so the policies would have been stored and never evaluated — the portal would have read every access log, the full staff roster and every pending invite while the meta-test reported "covered".

**Do** — Two policies only, on the two tables that actually have RLS. Pass **model names** (`Event`, `Tag`), not table names:

| Table | Operation | Why |
| --- | --- | --- |
| `audit_event` | `DenyPortalAccess` | RLS enabled with a permissive tenant policy; the restrictive deny composes correctly. |
| `clients_tag` | `DenyPortalAccess` | RLS enabled. Firm-wide vocabulary; leaking it exposes the firm's client taxonomy. |

Then add `PORTAL_DECISION_EXEMPT` to `apps/core/rls.py` — a mapping of table → **justification string** (empty justifications fail T-056):

| Table | Justification |
| --- | --- |
| `audit_accesslog` | RLS disabled by Phase-1 design (`apps/audit/models.py:174-178`: tenant is nullable so pre-auth and anonymous requests are still recorded). **`app_portal` holds no SELECT privilege on it — asserted by T-056.** |
| `audit_platformevent` | In `NON_TENANT_TABLES`; platform-level, pre-tenant, and **cross-tenant**. **No SELECT privilege — asserted by T-056.** |
| `audit_datasubjectrequest` | In `NON_TENANT_TABLES`; LGPD intake is a platform flow in 2a. **No SELECT privilege — asserted by T-056.** |
| `tenants_membership` | Tenancy root, deliberately unpoliced — the middleware reads it *before* `SET LOCAL ROLE`, as `app_runtime`, so the portal never needs it. **Gains a nullable `client_id` in T-059 and stays exempt** (exemption is evaluated before the client-column branch). **No SELECT privilege — asserted by T-056.** |
| `tenants_invite` | Tenancy root, same bootstrap reason. **No SELECT privilege — asserted by T-056.** |

> Revision 2 justified these as "bounded by the SELECT-only grant". That was **false** — the
> grant was `ON ALL TABLES`, so it bounded nothing. The allow-list in T-051 is what makes
> these justifications true, and T-056 is what keeps them true.

> **Do not "fix" this by enabling RLS on those five.** They have no permissive policy, so
> enabling RLS denies `app_runtime` outright — breaking login, Marco Civil logging and LGPD
> intake. Verified: firm reads drop to 0 and INSERT raises *"new row violates row-level
> security policy"*. That is the outage this todo exists to avoid. Widening these is a
> separate plan.

> **`audit_accesslog` needs no portal decision at all.** `apps/audit/middleware.py:64-69`
> writes in the **response phase**, and AccessLog is registered *outside* TenantMiddleware
> (`config/settings/base.py:85` vs `:92`) — so the INSERT happens after the tenant
> transaction has committed, in autocommit, as `app_runtime`, with `SET LOCAL ROLE` already
> reverted. No portal policy is involved in either direction. Revision 1's claim that INSERT
> *"flows through the existing permissive tenant policy"* was wrong twice over.

**Acceptance criteria** — Exactly 2 new policies. A portal session touching `audit_event` or `clients_tag` raises **`permission denied`**, *not* "0 rows": T-051's allow-list withholds SELECT and the privilege check fires **before** RLS is evaluated. The `DenyPortalAccess` policies are the **2b backstop** for when the grant widens — asserted statically by T-056, never exercised at runtime in 2a. Every exempt table has a non-empty justification. Portal access logging still works end to end.
**QA — happy**: portal access to both policed tables raises `permission denied`; access log still written → `.evidence/T-055-happy.txt`
**QA — failure**: empty justification fails T-056; applying `DenyPortalAccess` to `audit_accesslog` raises (T-052 guard) → `.evidence/T-055-failure.txt`
**Commit**: `feat(audit,clients): portal denials and justified exemptions`

---

#### T-056 — Portal coverage meta-test (**NON-NEGOTIABLE**)

**References** — `tests/isolation/test_rls_coverage.py` is the Phase-1 analogue. Fixes **B7**, **A3**. Oracle: *"If the meta-test cannot be written, the design does not stand up."*

**Do** — `tests/isolation/test_portal_rls_coverage.py`. For **every** table in `public` carrying `tenant_id` — enumerated from the catalog, **not** from a hand-written list:

- **the exempt list itself is pinned**: assert `set(PORTAL_DECISION_EXEMPT)` equals a literal `frozenset` of the six expected table names, written out in the test file. *`portal_decision_exemption()` matches with `fnmatchcase`, so a single entry `PORTAL_DECISION_EXEMPT["*"] = "anything"` exempts every table and T-056 stays **fully green** — count unchanged, privileges unchanged, RLS flags unchanged. One line would silently void a NON-NEGOTIABLE meta-test. Pinning makes every future exemption a deliberate two-file edit; that is the point, not an oversight.*
- **`PORTAL_DECISION_EXEMPT` is evaluated FIRST.** An exempt table is never subject to the client-column branch below. *Without this ordering, T-059 (Wave 2) adds a nullable `client_id` to `tenants_membership`, and Wave 1's non-negotiable meta-test then demands a portal policy on a table that has RLS disabled and that T-052 is specified to refuse — a Wave-2 commit turning a Wave-1 test red with no stated resolution.*
- carries `client_id` (or is `clients_clientcompany`) → assert a policy with `permissive='RESTRICTIVE'`, `roles={app_portal}`, `cmd='ALL'`, and a `qual` that:
  1. contains `NULLIF(` and `, true)`;
  2. **names `app.client_id`** — without this a copy-pasted *tenant* predicate passes every assertion while providing zero client isolation;
  3. **anchors on the expected column as the LEFT operand** — `re.match(rf"^\(\s*{re.escape(column)}\s*=", qual)`, where column is `client_id` (or `id` for `clients_clientcompany`). **A substring test here is decoration and MUST NOT be used**: verified against what PG16 actually stores, a wrong-column policy `(tenant_id = NULLIF(current_setting('app.client_id', true),'')::uuid)` passes a substring check on both `client_id` (it occurs inside the GUC literal) and `id` (a substring of `tenant_id`). That is the same defect this assertion exists to prevent, reproduced inside its own fix;
  4. **the same assertions applied to `with_check`** — non-null, not `'true'`, contains `NULLIF(` and `, true)`, names `app.client_id`, and **anchors on the expected column with the identical regex** `re.match(rf"^\(\s*{re.escape(column)}\s*=", with_check)`. *Do not write "names" here: `WITH CHECK (tenant_id = NULLIF(current_setting('app.client_id',true),'')::uuid)` satisfies every substring test, which is W1-BL-1 reappearing on the write side — the side that cannot be exercised at runtime in 2a.* Guard `with_check is not None` first: a policy authored without an explicit `WITH CHECK` stores `NULL` and `.strip()` would raise `AttributeError` instead of failing by name. *S9's `roles='{public}'` filter stops `test_the_policy_shape_is_not_merely_present` seeing portal policies, and the SELECT-only grant makes `WITH CHECK` unexercisable at runtime because the privilege check fires first. Without this, `WITH CHECK` ships completely unverified and a `WITH CHECK (true)` typo becomes a cross-client write hole the instant 2b widens the grant.*
- otherwise → assert a `DenyPortalAccess` policy whose **shape** is checked, not merely its presence: `permissive='RESTRICTIVE'`, `roles={app_portal}`, `cmd='ALL'`, and `qual.strip().lower() == 'false'` **and** the same for `with_check` (PG16 stores both as the literal `false`). *T-055 calls these the 2b backstop — "asserted statically by T-056, never exercised at runtime in 2a". Presence alone means a policy mangled to `USING (true)` passes here and is unreachable at runtime under the SELECT-only grant, so nothing catches it until 2b widens the grant. That is C9's finding, applied to `with_check` but not carried to the deny policies.*
- **privileges, both directions** (this is what makes `PORTAL_DECISION_EXEMPT` true rather than a label). **The oracle must be `ops/sql/roles.sql`, not `tests/conftest.py`.** Regex-extract the quoted names from that file's `FOREACH portal_table IN ARRAY ARRAY[...]` block, assert the extracted set equals `conftest.PORTAL_TABLES`, and use the extracted set as the oracle. *Using `PORTAL_TABLES` alone is tautological: `conftest` ISSUES the test-database grants from that same tuple, so adding `accounts_user` to it would grant SELECT and widen the expectation together — green, while production `roles.sql` is untouched and divergent.* Then for every table in `public`, assert `has_table_privilege('app_portal', t, 'SELECT')` is **true** iff `t` is in that set and **false** otherwise.
- **no write privilege anywhere**: assert `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE` and `REFERENCES` are all false for `app_portal` on every table in `public`. *Without this, `GRANT INSERT ON obligations_obligation TO app_portal` leaves T-056 green — and it would also falsify this plan's own premise that `WITH CHECK` is unexercisable in 2a because the privilege check fires first.*
- **every portal policy is RESTRICTIVE**: assert it for *all* policies whose `roles` include `app_portal`, not just the one found per table. *A stray PERMISSIVE `app_portal` policy ORs with the Phase-1 `{public}` tenant policy and drops TENANT isolation for portal sessions, while every existential shape assertion above still passes.* This is the only assertion that sees `accounts_user`, `mfa_authenticator` and `django_session`, since the tenant-scoped enumeration never reaches them.
- **for every table carrying any portal policy**: assert `relrowsecurity` **and** `relforcerowsecurity` are true. *This is the assertion that catches B2 — a policy on an RLS-disabled table is inert.*
- **once, globally**: assert `pg_has_role('app_runtime','app_portal','USAGE')` is **false** and `pg_has_role('app_runtime','app_portal','SET')` is **true**. *This is the assertion that catches B1.*

Assert the enumeration is **non-empty and equals exactly 14** — the test runs against the test database. *Do not write `in {13, 14}`: under that, losing a real `tenant_id` table while `core_tests_exampletenantmodel` is present would pass. The dev value of 13 is context, never an accepted value.* (`test_rls_coverage.py:166` is a non-empty guard, not a count assertion; this adds the count.) `core_tests_exampletenantmodel` carries no `client_id`, so give it a `PORTAL_DECISION_EXEMPT` entry — a deny policy on a test-only table is dead weight. A broken enumeration otherwise passes silently green.

Match with `fnmatch.fnmatchcase`, never `in`. The test DB contains `core_tests_exampletenantmodel` (`apps/core/tests/models.py:10`, installed by `config/settings/test.py:11`) — **14** `tenant_id` tables in test versus 13 in dev. Give it an explicit decision or the test fails on first run.

**Acceptance criteria** — Passes against the current schema. Each of these makes it fail, naming the cause: a new tenant+client table with no policy; a tenant predicate substituted for the client predicate; RLS disabled on a policed table; the grant re-made with default INHERIT; an empty justification.
**QA — happy**: `uv run pytest tests/isolation/test_portal_rls_coverage.py -v` → `.evidence/T-056-happy.txt`
**QA — failure**: all five mutations above, each observed red → `.evidence/T-056-failure.txt`
**Commit**: `test(isolation): portal policy coverage meta-test`

---

#### T-057 — Client-to-client denial suite

**References** — `tests/isolation/test_cross_tenant.py` is the model: raw SQL, so the assertion is about PostgreSQL and not about Django. Fixes **A6**.

**Do** — `tests/isolation/test_portal_client_isolation.py`. Call **`assert_isolated_role()`** (`tests/isolation/rolecheck.py`) at module top, as every existing module in `tests/isolation/` does — without it a `SET ROLE` leak lets this suite pass while running as `app_test`, which holds BYPASSRLS. Every test wraps its cursor work in an **explicit `transaction.atomic()`** — under `django_db(transaction=True)`, `SET LOCAL` outside a transaction emits a warning and **silently does nothing**.

1. **Positive control** — portal session, `app.client_id = A`, reads A's rows. *If this fails the suite proves nothing.*
2. **Read denial** — cannot see B's rows in the same tenant. Assert by **id**, not count.
3. **Fail-closed** — `app.tenant_id` set, `app.client_id` absent → 0 rows.
4. **Empty-string GUC** — `app.client_id = ''` → 0 rows (the `NULLIF` path).
5. **Write denial** — INSERT is refused. *In 2a this is `permission denied` (SELECT-only grant), not a policy violation — assert the actual behaviour, and note it becomes a policy violation in 2b.*
6. **Firm unaffected** — `app_runtime` with no `app.client_id` still sees the whole tenant. *This is the B1 regression test.*
7. **Role reverts** — after COMMIT the connection is `app_runtime` again (connection-reuse safety under `CONN_MAX_AGE`).

Parametrised across all 5 client-scoped tables plus `clients_clientcompany`.

**Acceptance criteria** — All pass; every negative has its positive control in the same module.
**QA — happy**: `uv run pytest tests/isolation/test_portal_client_isolation.py -v` → `.evidence/T-057-happy.txt`
**QA — failure**: drop one restrictive policy → tests 2 and 3 red for that table; restore → `.evidence/T-057-failure.txt`
**Commit**: `test(isolation): client-to-client denial suite`

---

### Wave 2 — Identity and context

#### T-058 — `current_client_id` and `client_context()`

**References** — `apps/core/tenancy.py` — mirror `tenant_context` exactly, including the explicit GUC restore *before* block exit rather than in `finally`.

**Do** — `current_client_id: ContextVar[UUID | None]`, `client_context(client_id)`, `MissingClientContext`. Write/read `app.client_id` via `set_config(..., true)`. **Do not** touch the Celery task bases — no portal-triggered task exists in 2a.

**Acceptance criteria** — Sets and restores the GUC; nesting restores the outer value; `None` raises rather than silently clearing.
**QA — happy**: nested context GUC assertions → `.evidence/T-058-happy.txt`
**QA — failure**: `client_context(None)` raises → `.evidence/T-058-failure.txt`
**Commit**: `feat(core): client context var and GUC management`

---

#### T-059 — Extend `Membership` **and fix the four firm-side queries it breaks**

**References** — Fixes **B5**, the second-most dangerous finding. Closes the parent plan's T-027 thread (`client_owner`/`client_collaborator` seeded but unassignable).

> **Revision 1 claimed existing `Membership` queries "remain correct for firm-side reads".
> That was wrong at all six call sites — none filters on `client`.** One is a privilege
> escalation: a portal user browsing the **firm** host passes the middleware's `.exists()`,
> receives `app.tenant_id`, runs as `app_runtime` with **no** restrictive policy, and reads
> the firm's entire client base — reopening precisely the hole Phase 2a exists to close.

**Do** — schema:
- `CLIENT_OWNER` / `CLIENT_COLLABORATOR` added to `tenants.TenantRole`, values matching `authz.Role` **exactly** (a mismatch fails closed but silently).
- Nullable `client` FK with the composite `(tenant_id, client_id)` via `add_tenant_composite_fk`.
- `UniqueConstraint(["user","tenant","client"], nulls_distinct=False)` replacing `(user, tenant)`. NULL client is the firm-side sentinel.
- `CheckConstraint`: firm roles ⇒ `client_id IS NULL`; client roles ⇒ `client_id IS NOT NULL`.

**In the same commit**, the **six** call-site fixes:

| Site | Fix |
| --- | --- |
| `apps/tenants/middleware.py:142-146` | add `client__isnull=True` — **the escalation** |
| `apps/core/access.py:48-52` (`PlatformScopedManager.for_user`) | add `client__isnull=True`; otherwise `Tenant/Invite/AccessLog.objects.for_user` all widen |
| `apps/accounts/invites.py:161-165` | pass `client=None` explicitly; otherwise `get_or_create` can raise `MultipleObjectsReturned` → 500 |
| `apps/clients/models/assignment.py:97-101` | add `client__isnull=True`; otherwise a client-role user passes the firm-roster test and can be `assigned_to`, which `_is_attached_to` consults |
| `apps/accounts/views.py:210-215` (`team_view`) | add **outer** `client__isnull=True` — portal users would otherwise appear on the firm's team page |
| `apps/obligations/views.py:190-196` (`_members`) | add **outer** `client__isnull=True` — portal users would otherwise be offered in the obligations assignee filter |

> **Why fixing `access.py` does not cover the last two.** `for_user` returns
> `self.get_queryset().filter(tenant_id__in=tenant_ids)`. Adding `client__isnull=True` narrows
> the **subquery** — *which firms the caller belongs to*. When the manager's model **is**
> `Membership`, the **outer** queryset still returns client-role rows. Both sites need their
> own filter.

**Grep guard** — must match, **case-insensitively**:
`(?i)\b(membership|membership_model)\.objects\.(filter|for_user|get_or_create|exclude)\(`
outside `apps/authz/`, each hit mentioning `client`. Ship a **guard self-test** asserting the
pattern matches `apps/core/access.py:48`.

> Two earlier versions of this guard were blind to sites this very todo fixes. Guarding only
> `.filter(` missed the two `.for_user(` sites; a case-sensitive `Membership\.` misses
> `membership.objects.filter(` — the `django_apps.get_model(...)` + lowercase-local idiom at
> `apps/core/access.py:48`. Two of the eight call sites use that idiom, so it is an
> established convention, not an edge case. A guard that cannot see the code it guards is
> the same defect class as a test that passes while asserting nothing.

**`apps/accounts/mfa.py:47` — DECIDED, not deferred.** Keep the behaviour: MFA becomes
mandatory for MEI portal owners, which is the safe direction for accounts holding tax data.
Mark it with an inline `# CLIENT_SCOPE_OK: <reason>` comment that the guard honours — the
same shape as the existing `PLATFORM_QUERY_OK` convention. *Revision 2 asked for a decision
"either way" while also requiring the guard to pass; leaving it unchanged would have made
this todo unfinishable and tripped operating rule 5 mid-wave.*

**Acceptance criteria** — Migration applies, and **T-056 is still green afterwards** (`tenants_membership` now carries `client_id` but remains exempt). The Django-level `client` FK uses **`on_delete=models.CASCADE`**, matching the `ON DELETE CASCADE` hard-coded by `add_tenant_composite_fk` (`_composite_fk.py:54`). A user holds a firm role in tenant A and a client role in tenant B. Two firm-side rows for one `(user, tenant)` rejected. Role/client CHECK violations rejected. **One test per call site proving a client-role membership does not leak into firm-side behaviour** — especially: a portal user hitting the firm host gets `PermissionDenied`. Grep guard passes.
**QA — happy**: cross-tenant memberships + all **six** call-site tests → `.evidence/T-059-happy.txt`
**QA — failure**: revert the middleware fix → the escalation test goes red; duplicate firm membership raises; `owner` with a client raises `CheckViolation` → `.evidence/T-059-failure.txt`
**Commit**: `feat(tenants): portal identity via client-scoped membership`

---

#### T-060 — `role_of()` resolves client roles

**References** — `apps/authz/services.py`. Must land **with or immediately after** T-059: `role_of`'s `.first()` on an unordered queryset returns the oldest row by UUIDv7 pk — deterministic but arbitrary — until the `client=` filter exists.

**Do** — `role_of(user, tenant_id)` additionally filters `client=current_client_id.get()`; with no client context that is `client=None`, i.e. firm-side behaviour unchanged — also the correct result for Celery tasks and management commands, where no client context exists. Extend `_is_attached_to()` with one branch: when a client context is set, attachment means `_client_id_of(obj) == current_client_id.get()` — the existing helper (`services.py:242-248`), **not** `obj.client_id`, because for `ClientCompany` the identity is `obj.pk` and the naive form fails closed by denying a portal user their own company. **No second permission function.**

**Acceptance criteria** — `can()` resolves `client_owner` grants for a portal user. Firm-side resolution byte-identical to Phase 1 (full authz suite unmodified). `can()` still fails closed on unknown capabilities. The `role ==` grep guard still passes.
**QA — happy**: `uv run pytest tests/authz/ -v` incl. portal cases → `.evidence/T-060-happy.txt`
**QA — failure**: portal user denied `users.create` → `.evidence/T-060-failure.txt`
**Commit**: `feat(authz): resolve client roles through the existing can() path`

---

### Wave 3 — Request path (V2 must be resolved first)

#### T-061 — `PortalMiddleware`

**References** — `apps/tenants/middleware.py`. Fixes **B6**.

**Do** — `apps/portal/middleware.py::PortalMiddleware`, copying `TenantMiddleware`'s structure verbatim including the `StreamingHttpResponse` `TypeError` guard and template-render-inside-transaction:

1. **Split routing from authorization exactly as `TenantMiddleware` does** — `_resolve_tenant` (`:108-121`) resolves the host; `_grant_context` (`:123-149`) returns `None` for anonymous users rather than raising. **Revision 1 required an authenticated client-role membership before the login page could render, which 403s the login page itself.**
2. Anonymous portal requests run as **`app_runtime` with both GUCs empty** — never `app_portal` with no `client_id`.
3. Authenticated: require an active **client-role** Membership in that tenant, else `PermissionDenied`.
4. **Establish the full request context — all five, explicitly.** Revision 5 named only the two GUCs, leaving every ContextVar consumer unserved:
   - `TenantScopedManager.get_queryset()` returns **`.none()`** when `current_tenant_id` is unset — every portal ORM read yields **zero rows silently**.
   - `role_of()` reads `current_tenant_id`; unset ⇒ `can()` denies everything.
   - T-060 filters `client=current_client_id.get()`, but **no todo assigned the setter** — a portal user's `client_owner` grant would never resolve.
   - `AccessLogMiddleware` and `RateLimitMiddleware` read `request.tenant`; unset costs Marco Civil attribution and keys the bucket on `tenant:None`.

   Required, in order:
   1. Resolve the `Tenant` and assign `request.tenant` **before** `SET LOCAL ROLE`, while still `app_runtime`.
   2. Assign `request.client` likewise.
   3. Inside the outer atomic block: `SET LOCAL ROLE app_portal`.
   4. Set `app.tenant_id` and `app.client_id` **from `membership.client_id`**.
   5. Set `current_tenant_id` **and** `current_client_id` with `token` / `reset()` in a `finally`, mirroring `apps/tenants/middleware.py:157-169`.

   > **Do NOT call `tenant_context()` / `client_context()` inside the portal transaction.** They open a nested savepoint whose `_write_guc(previous)` restore is the savepoint-merge hazard documented at `apps/core/tenancy.py:78-90`. Set the ContextVars directly. Decided — not a fork.
5. Host parsing uses a new `portal_slug_from_host()` that **requires and strips** the `-portal` suffix, returning `None` when absent. `slug_from_host` returns `labels[0]` verbatim, so reusing it would resolve the tenant `"acme-portal"`, which does not exist.
6. State the `_is_non_atomic` decision (`middleware.py:74-90`): a `@non_atomic_requests` view gets **no transaction**, therefore no `SET LOCAL ROLE`. Assert the portal urlconf contains none, or replicate the guard.
7. **`MIDDLEWARE` order — concrete and positional.** Both are registered; exactly one is active per host. The order is:

   ```
   i = MIDDLEWARE.index("apps.accounts.middleware.MFAEnforcementMiddleware")
   MIDDLEWARE[i+1] == "apps.portal.middleware.HostDispatchMiddleware"   # T-062
   MIDDLEWARE[i+2] == "apps.portal.middleware.PortalMiddleware"
   MIDDLEWARE[i+3] == "apps.tenants.middleware.TenantMiddleware"
   MIDDLEWARE[i+4] == "apps.security.middleware.RateLimitMiddleware"
   ```

   > **The dispatcher must precede `RateLimitMiddleware` — a security requirement, not tidiness.** `apps/security/ratelimit.py:92` matches `RATELIMIT_PUBLIC_POST_URL_NAMES` by `url_name` via `resolve(..., urlconf=getattr(request, "urlconf", None))`. Django sets `ROOT_URLCONF` at the top of `BaseHandler.get_response` and honours `request.urlconf` only in `_get_response` — **after** every request-phase middleware. A dispatcher running after RateLimit means the portal login POST resolves against `ROOT_URLCONF`, and the credential rate limit **silently stops applying to the portal login form**. `_is_non_atomic` (item 6) reads the same attribute.

   Assert exactly that. *Revision 4 asked for `MIDDLEWARE.index(Portal) == MIDDLEWARE.index(Tenant)`, which is **impossible** for two distinct list entries — it replaced a wrong number with an unimplementable structure. Anchoring on `MFAEnforcementMiddleware` stays robust to future insertions without asserting a bare index.*

   > **Portal must precede Tenant, and neither may sit at index 11.** That is
   > `MFAEnforcementMiddleware`; putting the portal outside it drags `_must_enrol`
   > (`apps/accounts/middleware.py:36-48`) *inside* the portal transaction, where
   > `requires_mfa()` hits `tenants_membership` and `is_mfa_enabled()` hits
   > `mfa_authenticator` — both denied. Verified: `permission denied for table
   > tenants_membership`.

   **The `TenantMiddleware` no-op belongs to THIS todo — specify it, do not infer it.**
   First statement of `TenantMiddleware.__call__`:

   ```python
   # Placed immediately AFTER the existing _is_non_atomic guard, never before it:
   # get_host() raises DisallowedHost, and /healthz is @non_atomic_requests and must keep
   # serving on hosts outside ALLOWED_HOSTS without touching the database.
   if portal_slug_from_host(request.get_host()) is not None:
       return self.get_response(request)   # touch neither the ContextVar nor the DB
   ```

   Mirror it in `PortalMiddleware` for non-portal hosts.

   > **Explicitly NOT `_run_without_database_context`.** It sets `current_tenant_id` to
   > `None` **and `request.tenant` to `None`** — it does *not* call `_apply_guc` (that is
   > reached only from `_run_in_tenant_context`, `apps/tenants/middleware.py:163`). Clobbering either is enough: `TenantScopedManager.get_queryset()` returns **`.none()`**
   > when `current_tenant_id` is `None` (`apps/core/models.py:20-25`), and `AccessLogMiddleware`
   > / `RateLimitMiddleware` read `request.tenant`. Separately — and this is the savepoint
   > hazard's real owner — `_run_in_tenant_context(request, None)` calls `_apply_guc(None)`
   > at `:163`; since `PortalMiddleware` owns the outer transaction that block is a
   > **savepoint**, and `set_config(..., true)` **merges upward on `RELEASE SAVEPOINT`** —
   > verified live: after release `app.tenant_id` reads empty. It would stay cleared for the rest of the request and the Phase-1
   > permissive policy would return **zero rows silently** — worse than a 500, because
   > nothing fails loudly. Had `_resolve_tenant` run instead, `Tenant.objects.filter()` as
   > `app_portal` raises `permission denied for table tenants_tenant`.
   >
   > The plan previously applied the savepoint-merge insight only to "Portal nested inside
   > Tenant" — never to the reverse, which is the nesting its own layout produces.

   > **The entire write-path safety argument depends on this position.** Registering
   > `PortalMiddleware` earlier — the natural instinct, since it does host work — puts it
   > *outside* `SessionMiddleware` (`base.py:71`), and every authenticated portal request
   > then dies with `permission denied for table django_session` under the SELECT-only
   > grant. At index 11 the writes are safe: `django_session` is saved in
   > `SessionMiddleware`'s response phase, after COMMIT, in autocommit as `app_runtime`;
   > `accounts_user.last_login` is written during a login POST that is **anonymous at
   > middleware entry**, so item 2 runs it as `app_runtime`; `audit_platformevent` is
   > buffered by `PlatformEventMiddleware` (`base.py:81`) and flushed outside the
   > transaction; `audit_accesslog` is written in the response phase, outside the tenant
   > transaction entirely.
   >
   > Nesting the two is also unsafe: `SET LOCAL ROLE` **merges upward on `RELEASE
   > SAVEPOINT`** (verified), so `app_portal` would leak into `_render_inside_transaction`
   > and `_reject_streaming` — the same behaviour already documented at
   > `apps/core/tenancy.py:78-90`.

8. **Refuse `/admin/` on the portal host before opening the transaction.** `AdminTenantMiddleware` sits after `RateLimitMiddleware` and therefore *inside* whichever middleware owns the transaction (stated structurally — the bare index drifts with every insertion, which is how revision 6 shipped a wrong one) — and gates only on `request.path_info.startswith("/admin/")`, **independent of `request.urlconf`**, so T-062's urlconf swap does not skip it. `GET /admin/` on a portal host reaches `Tenant.objects.filter(...).exists()` and raises `permission denied for table tenants_tenant` (verified) — an unhandled 500 where the firm host returns 404. `PortalMiddleware` returns 404 for the admin prefix *before* `SET LOCAL ROLE`.

9. **Generalise the middleware-order system checks over BOTH transaction owners.**
   `apps/core/checks.py:125-150` keys on the literal `apps.tenants.middleware.TenantMiddleware`
   and enforces that `AccessLogMiddleware` and `PlatformEventMiddleware` precede it — the
   exact ordering this todo's write-path argument depends on. `PortalMiddleware` opens an
   equivalent transaction and is invisible to `core.E005/E006/E007`. Introduce
   `TRANSACTION_MIDDLEWARE = (TENANT_MIDDLEWARE, PORTAL_MIDDLEWARE)` and take `min()` of the
   present indices; **reuse the existing E005/E006/E007 ids**. **E005 must fire when *either*
   owner is absent, not only when both are** — and fold the positional invariant (dispatcher
   before portal before tenant) into the same family so it is enforced at **startup** across
   dev/test/prod, not only by a test.

   > Without startup enforcement, BL-2's silent failure stays reachable: `TenantMiddleware`'s
   > no-op is unconditional on portal hosts, so any settings module omitting or mis-ordering
   > `PortalMiddleware` yields a portal request with no tenant context, no portal context and
   > **no error** — `TenantScopedManager` returns `.none()`. `ci.yml:114-121` runs
   > `manage.py check` against three settings modules; that is where this must bite. Without it, an audit INSERT
   inside the portal transaction raises `permission denied` under the SELECT-only grant — a
   500 on every portal request.

10. Docstring records the pgbouncer session-pooling assumption (constraint 3), the `RESET ROLE` threat model (constraint 7), and the savepoint-merge behaviour above.

**Acceptance criteria** — the four-way positional assertion in item 7 holds. `core.E005/E006/E007` fire for `PortalMiddleware` exactly as for `TenantMiddleware` (prove it: move `AccessLogMiddleware` after the portal and observe the check fail). A portal request issues **exactly one `BEGIN`**, and `app.tenant_id` is still set at response time — the `TenantMiddleware` no-op regression test. `GET /admin/` on a portal host returns **404, not 500**. Anonymous request reaches the login page. **During the login POST `current_user` is `app_runtime` and the session write succeeds.** Authenticated portal request runs as `app_portal` with both GUCs. A firm-only user gets `PermissionDenied`. Streaming raises `TypeError`. No role or GUC leaks to the next request on the same connection.
**QA — happy**: anonymous 200 + authenticated `current_user`/GUC assertions → `.evidence/T-061-happy.txt`
**QA — failure**: firm-only user denied; connection-reuse proves no leak → `.evidence/T-061-failure.txt`
**Commit**: `feat(portal): portal middleware with app_portal role and client GUC`

---

#### T-062 — Host dispatch and the tamper guard

**References** — Constraint 5. Fixes **A5**.

**Do** — Thin dispatch middleware selecting `request.urlconf` by host: `<slug>-portal.<domain>` → portal chain; otherwise today's path unchanged.

> **Name which allauth URLs the portal urlconf includes.** `MFAEnforcementMiddleware`
> (`base.py:89`) calls `reverse("mfa_activate_totp")`. Because T-062 swaps
> `request.urlconf` per host, omitting allauth's MFA tree makes every portal request by a
> non-enrolled user a `NoReverseMatch` **500** — and `is_exempt_path` exempts `/accounts/`,
> so that tree must be reachable on the portal host. At minimum: login, logout, password
> reset, MFA enrolment. Two guards, because one is not enough:
- **Grep guard**: `set_config('app.client_id'` / `current_client_id.set(` may appear only in `apps/core/tenancy.py` and `apps/portal/middleware.py`.
- **Behavioural test**: a request carrying `?client_id=<other>`, an `X-Client-Id` header, and a forged session key still resolves to `membership.client_id`. *The grep guard checks file location, not provenance — moving a session read inside the permitted module would leave it green.*

**Acceptance criteria** — Both hosts route correctly. The portal urlconf mounts the allauth tree at the **same paths** as `ROOT_URLCONF`, so `RATELIMIT_PUBLIC_POST_URL_NAMES`, `ACCESS_LOG_EXEMPT_PREFIXES` and `apps/accounts/mfa.py:36` `EXEMPT_PREFIXES` keep matching. **A POST to the portal login form is rate-limited at `RATELIMIT_LOGIN_EMAIL`** — prove it. Both guards pass, and each fails when its specific violation is introduced. A tenant slug ending in `-portal` is rejected at creation. `reverse("mfa_activate_totp")` resolves under the portal urlconf.
**QA — happy**: routing + both guards → `.evidence/T-062-happy.txt`
**QA — failure**: forged `client_id` ignored; a client-id write added to a view fails the grep guard → `.evidence/T-062-failure.txt`
**Commit**: `feat(portal): host dispatch with client-id tamper guards`

---

#### T-063 — Portal login page (the tracer bullet)

**References** — allauth is email-only with mandatory verification. Reuse it; no second auth backend.

**Do** — A portal base template (no PWA assets — that is 2b) and one authenticated view rendering the client's `legal_name`, `cnpj` and obligation count.

> **Template constraint, verified by running this render path as `app_portal`.** The
> six-table allow-list is *exactly* sufficient: `clients_clientcompany`,
> `obligations_obligation`, and `clients_onboardingitem` (reached by `is_ready` /
> `readiness_score` through `onboarding_items`). `{{ request.tenant.name }}` and
> `{{ request.user.email }}` cost **zero queries** — both objects are fully materialised
> before the transaction opens. **Forbidden in portal templates**: `{{ perms.* }}`
> (`auth_permission`), and any reach into `tenants_tenant`, `mfa_authenticator` or
> `clients_onboardingitemtemplate` — all denied. Confirm `SESSION_COOKIE_DOMAIN` stays `None` — host-only cookies are what defeat cookie-tossing between sibling hosts. Add the portal origin to `CSRF_TRUSTED_ORIGINS` (no wildcards — `core.E003`). This is now **two entries per tenant**, each new firm needing a redeploy; acceptable for one hand-managed staging tenant in 2a, but automating it is a named Phase-2b task.

**Acceptance criteria** — A client-role user logs in at `<slug>-portal.<domain>` and sees exactly their own company. Obligation count matches only their rows. A firm-side user cannot log in there. `SESSION_COOKIE_DOMAIN` still `None`.
**QA — happy**: end-to-end login rendering the right company → `.evidence/T-063-happy.txt`
**QA — failure**: a second client in the same firm never appears → `.evidence/T-063-failure.txt`
**Commit**: `feat(portal): authenticated portal landing page`

---

### Wave 4 — Verification

#### T-064 — Mutation matrix

**References** — Phase-1 practice. Every control must be falsifiable.

**Do** — In an isolated copy, remove each control **one at a time**:

| Mutation | Expected |
| --- | --- |
| **`GRANT app_portal TO app_runtime WITH INHERIT TRUE`** — a plain re-grant is a verified **no-op** and would make this mutation vacuous | T-056 role assertion red; T-057 test 6 red — *the B1 regression test* |
| Drop restrictive policy on `obligations_obligation` | T-057 tests 2–3 red; T-056 red |
| Remove `AS RESTRICTIVE` | T-056 shape assertion red |
| Remove `NULLIF(...)` | T-057 test 3 red |
| Substitute the **tenant** predicate for the client predicate | T-056 `app.client_id` assertion red |
| **Portal `WITH CHECK` → `true`** | T-056 `with_check` assertion red — static, so the SELECT-only grant does not mask it |
| **Column swapped to `tenant_id`, GUC left as `app.client_id`** | T-056 column-anchor assertion red. *Green today under a substring test — this is the mutation that proves the anchor is load-bearing* |
| **`DenyPortalAccess` `USING (false)` → `USING (true)`** | T-056 branch-B shape assertion red |
| **add a 7th entry to `conftest.PORTAL_TABLES`** | T-056 roles.sql-vs-conftest equality red — proves the oracle is the production file |
| **`PORTAL_DECISION_EXEMPT["*"] = "x"`** | T-056 pinned-exempt-set assertion red |
| **`GRANT INSERT ON obligations_obligation TO app_portal`** | T-056 write-privilege assertion red |
| **portal policy `AS RESTRICTIVE` → permissive** | T-056 all-portal-policies-restrictive assertion red |
| Disable RLS on a policed table | T-056 `relrowsecurity` assertion red |
| Remove `SET LOCAL ROLE` from the middleware | T-061 red; T-057 test 7 red |
| **Forge `client_id` via query param / header / session** | T-062 behavioural test red |
| Remove the Membership CHECK | T-059 failure QA red |
| Revert the middleware `client__isnull=True` | T-059 escalation test red |

**Any mutation that changes nothing is a defect** — that control is decoration and must be fixed or removed.

**Acceptance criteria** — Every row produces the expected red, recorded with exact failing test ids.
**QA — happy**: full matrix with observed outcomes → `.evidence/T-064-happy.txt`
**QA — failure**: any no-op mutation documented as a defect with remediation → `.evidence/T-064-failure.txt`
**Commit**: `test(isolation): mutation evidence for portal controls`

---

#### T-065 — Deploy to staging and verify on real infrastructure

**References** — V1 and V2 are already resolved by this point; this todo only executes them.

**Do** — Run the documented superuser `psql` step to create `app_portal` on the existing cluster (V1). Deploy, migrate, and run both isolation suites **against the deployed cluster** (Phase 1's precedent: 41 passed in 283s). Confirm the Caddy site block serves `<slug>-portal.<domain>` (V2).

**Acceptance criteria** — All suites green against staging. `app_portal` exists with the non-inheriting grant. A portal login works over HTTPS. Phase-1 assertions still hold at the S9 baseline.
**QA — happy**: staging suite output + live portal login → `.evidence/T-065-happy.txt`
**QA — failure**: cross-client read against staging returns 0 rows → `.evidence/T-065-failure.txt`
**Commit**: `chore(ops): deploy portal isolation to staging`

<!-- END TODOS -->

---

## Final verification wave

1. `uv run ruff check . && uv run ruff format --check .`
2. `uv run mypy .` — zero suppressions
3. `uv run pytest` — Phase-1 assertions hold at the S9 baseline
4. `uv run python manage.py makemigrations --check --dry-run`
5. Grep guards: `role ==`, client-id writes, `Membership.objects.filter`
6. T-064 matrix reviewed — every control falsifiable
7. Adversarial re-review (Momus + Oracle), as Phases 0–1 received

## Success criteria

1. A portal user authenticates at `<slug>-portal.<domain>` and sees exactly their own client.
2. A portal session with no `app.client_id` reads **zero** rows — proven at the DB layer.
3. A portal session cannot read another client's rows in the same tenant — proven at the DB layer, with positive controls.
4. **The firm still sees everything it saw before** — `pg_has_role('app_runtime','app_portal','USAGE')` is false and firm-side row counts are unchanged.
5. A portal user hitting the **firm** host is refused.
6. The meta-test fails on: a missing portal decision, a tenant predicate substituted for a client predicate, RLS disabled on a policed table, and an inheriting grant.
7. Every control in T-064 is falsifiable.
8. Zero `# type: ignore`, `cast(`, `# pragma: no cover`.
9. All of the above verified **on staging**.

**Phase 2b must not begin until criteria 4 and 6 are green.** Criterion 4 is the one that
would have taken the product down; criterion 6 is the one that keeps it that way.

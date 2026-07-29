# Phase 2a — review receipts

## Round 1 — both reviewers REJECT

Plan revision 1 went to Momus (`bg_e6e3e154`) and Oracle (`bg_5d852fb1`) in parallel, the
same dual-review pairing that caught ~80 defects across Phases 0–1.

Oracle did not reason about the worst finding — it **stood up a PostgreSQL 16.14 cluster and
reproduced it**.

### Blocking findings and their fixes

| # | Finding | Fix in revision 2 |
| --- | --- | --- |
| **B1** | `GRANT app_portal TO app_runtime` with default `INHERIT TRUE` makes the RESTRICTIVE portal policies bind `app_runtime` too. PostgreSQL matches RLS `TO <role>` by **privilege inheritance, not identity**. Reproduced: firm-side reads returned **0 rows instead of 2**. Every client table would have gone invisible to the accounting firm the moment T-053 applied. `NOINHERIT` on the role does *not* help — the flag that matters is on the grant edge. | T-051 now `GRANT ... WITH INHERIT FALSE, SET TRUE`. S2 asserts `has_privs_of_role` false / `pg_has_role(...,'SET')` true. T-056 asserts it globally; T-057 test 6 and a T-064 mutation regression-test it. |
| **B2** | 5 of T-055's 7 tables have **RLS disabled** (`audit_accesslog`, `audit_platformevent`, `audit_datasubjectrequest`, `tenants_membership`, `tenants_invite` — all in `NON_TENANT_TABLES`). A policy on an RLS-disabled table is **stored but never evaluated**. The portal would have read every access log, the full staff roster and all pending invites while the meta-test reported "covered" — the same shape as the Phase-1 PII guard whose regex could never match. Found independently by **both** reviewers. | T-055 keeps only `audit_event` + `clients_tag`. The other 5 move to `PORTAL_DECISION_EXEMPT` with mandatory justifications. T-052 now **raises** if a portal policy targets an RLS-disabled table. T-056 asserts `relrowsecurity` + `relforcerowsecurity`. |
| **B3** | T-055 broke 3 Phase-1 isolation tests (`test_the_tenancy_root_tables_are_deliberately_unpoliced`; the `cmd == "ALL"` loop; the `NULLIF`/`, true)` shape assertions), making S9 "41 tests unmodified" unachievable. | S9 rewritten to permit exactly one sanctioned edit — a `roles = '{public}'` filter on `_policies()` — and to re-baseline the count. Stated openly rather than left to be discovered mid-wave. |
| **B4** | T-055's rationale was factually wrong: `audit_accesslog` has **no** permissive tenant policy, and `AccessLogMiddleware` writes in the **response phase**, outside the tenant transaction, as `app_runtime`, with `SET LOCAL ROLE` already reverted at COMMIT. No portal policy is involved in either direction. | Rationale deleted and replaced with the correct mechanism. `audit_accesslog` needs no portal decision at all. |
| **B5** | Revision 1 claimed existing `Membership` queries "remain correct for firm-side reads". **Wrong at all six call sites** — none filters on `client`. `apps/tenants/middleware.py:142-146` is a **privilege escalation**: a portal user browsing the *firm* host passes `.exists()`, gets `app.tenant_id`, runs as `app_runtime` with no restrictive policy, and reads the firm's entire client base — reopening the exact hole Phase 2a exists to close. | T-059 now ships four call-site fixes in the same commit as the schema change, one test per site, plus a `Membership.objects.filter` grep guard. A T-064 mutation reverts the middleware fix and expects red. |
| **B6** | T-061 required an authenticated client-role Membership before rendering — which **403s the login page itself**, contradicting T-063. | T-061 adopts `TenantMiddleware`'s resolve/grant split verbatim; anonymous portal requests run as `app_runtime` with empty GUCs. The `_is_non_atomic` gap is stated explicitly. |
| **B7** | T-056 asserted only that `qual` contained `NULLIF(` and `, true)` — so a copy-pasted **tenant** predicate would pass every assertion while providing zero client isolation. It also never checked RLS was enabled (how B2 slips through) or that the grant does not inherit (how B1 slips through). | T-056 gains three assertions: the qual names `app.client_id`; it names the expected column; `relrowsecurity`+`relforcerowsecurity` are true. Plus the global `has_privs_of_role` check. |
| **B8** | T-065 floated "an idempotent migration or a manual step" for creating `app_portal` on an already-initialised cluster. The migration route is **impossible** — `app_migrator` is `NOCREATEROLE` and the grant needs ADMIN OPTION. | Promoted to **verify-first gate V1**, ahead of Wave 1. Documented superuser `psql` step (as CI already does) plus a `core.E0xx` startup check. |
| **B9** | `portal.<slug>.<domain>` is four labels and **cannot get a production certificate** — a public wildcard covers one label and no CA issues `*.*.example.com`. | Promoted to **verify-first gate V2**, ahead of Wave 3. Host scheme changed to `<slug>-portal.<domain>`: two labels, covered by the existing wildcard, matched by the existing 2-label `slug_from_host` with no extension, still a distinct host for `__Host-` cookies. |

### Advisories adopted

- **A1 (strong)** — T-051 granted full DML. Policies being `FOR ALL` and *grants* being full DML are separate decisions; revision 1 conflated them, so a portal user could `DELETE` their own DAS obligations. Now **SELECT-only** in 2a (Decided constraint 8), widened in 2b.
- **A2** — Threat model stated explicitly (constraint 7): `SET LOCAL ROLE` is not a boundary against arbitrary SQL — `RESET ROLE` escapes it (verified). It defends against forgotten ORM filters, not injection.
- **A3** — `core_tests_exampletenantmodel` added to the inventory; the **test** DB has 14 `tenant_id` tables versus 13 in dev, so T-056 would have failed on first run.
- **A4** — Off-by-one task ids in the TL;DR and S2–S6 corrected.
- **A5** — T-064's "client_id from session" mutation was a **no-op** (the grep guard checks file location, not provenance). T-062 gains a behavioural test with a forged `?client_id=`/header/session.
- **A6** — T-057 tests now wrap cursors in explicit `transaction.atomic()`; `SET LOCAL` outside a transaction warns and silently does nothing.
- **A7** — `CSRF_TRUSTED_ORIGINS` per-tenant scaling recorded in T-063.

### Estimate

Revised **4–6 days → 6–9 days**. Oracle judged the original optimistic by 1.5–2×, with
Wave 2 the underestimate: the schema change is small, but four call-site edits plus tests
plus `role_of`/`_is_attached_to` is a full day alone.

### What I got wrong, for the record

Two of these were mine to catch and I did not:

1. **B5** — I asserted the Membership queries were fine without enumerating them. Six call
   sites, six wrong, one a privilege escalation. I should have grepped before claiming.
2. **B2** — I built T-055 on the assumption that `audit_accesslog` had a permissive tenant
   policy. `apps/core/rls.py` lists it in `NON_TENANT_TABLES` and `apps/audit/models.py:174-178`
   documents the opposite *in prose*. Both reviewers found it independently.

B1 I would not have found without a live cluster; it contradicts the intuitive reading of
`TO <role>`.

---

## Round 2 — Momus OKAY, Oracle REJECT

Momus verified all 21 file references and every todo's completeness: **OKAY**.
Oracle again worked empirically on a live PostgreSQL 16.14 and returned **REJECT** with 9
new blocking findings. All accepted; none argued down.

| # | Finding | Fix in revision 3 |
| --- | --- | --- |
| **C1** | **`has_privs_of_role()` is not SQL-callable** — it is a C internal. It appeared in S2, T-051, T-056, the V1 startup check and success criterion 4. Every one would have raised `ProgrammingError` at runtime; the startup check would have failed at boot. The headline defence against B1 was unverifiable. | All 5 sites now `pg_has_role('app_runtime','app_portal','USAGE')` must be **false**, `...,'SET'` **true**. `'MEMBER'` is true in both configurations and is explicitly forbidden in a note. |
| **C2** | **The B1 mutation was a no-op.** A plain `GRANT app_portal TO app_runtime` against an existing membership emits a notice and leaves INHERIT unchanged — so T-051's failure QA and T-064's row 1 could never go red. A falsification test that cannot fail, in the plan whose whole safety argument is falsifiability. | Mutation is now explicit `GRANT ... WITH INHERIT TRUE` (verified: flips USAGE to true, firm reads drop to 0), restored with `WITH INHERIT FALSE`. |
| **C3** | B5's four call sites were **incomplete** — there are six. `apps/accounts/views.py:210-215` and `apps/obligations/views.py:190-196` use `.for_user(`, which the specified guard (`Membership.objects.filter(`) is worded to skip: guard green, sites broken. Portal users would appear on the firm's team page and in the obligations assignee filter. Fixing `access.py` does **not** cover them — `client__isnull=True` narrows the *subquery*, while the *outer* queryset still returns client rows when the manager's model is `Membership`. | Both sites added with outer filters; guard widened to `(filter\|for_user\|get_or_create\|exclude)`. |
| **C4** | T-059 **could not satisfy its own acceptance criteria**: `apps/accounts/mfa.py:47` trips the guard, yet the todo demanded both "record the decision either way" and "grep guard passes". Second-order: `MFAEnforcementMiddleware` calls `reverse("mfa_activate_totp")`, so a portal urlconf omitting allauth's MFA tree is a `NoReverseMatch` 500 on every portal request by a non-enrolled user. | Decision made *in the plan* — keep MFA for portal users, mark with `# CLIENT_SCOPE_OK:`. T-062 must name which allauth URLs the portal urlconf includes. |
| **C5** | **`PORTAL_DECISION_EXEMPT` was a relabelled leak** — and wider than the five tables. `GRANT SELECT ON ALL TABLES` bounded nothing, so `app_portal` also had SELECT on `accounts_user` (password hashes), **`mfa_authenticator` (TOTP secrets)**, `account_emailaddress` and `django_session`. T-056 never sees them because it iterates only `tenant_id` tables. | Grant **inverted to an allow-list**: SELECT on exactly the 6 tables the portal needs, no `ALTER DEFAULT PRIVILEGES`. T-056 asserts `has_table_privilege` in **both** directions. Justification strings rewritten from "bounded by the grant" (false) to "no SELECT privilege; asserted by T-056" (true). |
| **C6** | **The grant reached no table the app or tests use.** `ALTER DEFAULT PRIVILEGES` without `FOR ROLE` covers only the executing superuser's tables (dev tables are `app_migrator`'s, test tables `app_test`'s); `ON ALL TABLES` ran against an empty init-hook database; and `tests/conftest.py:42-69` grants only `RUNTIME_ROLE`. Every portal test would have died with `permission denied` **before reaching a policy** — and T-057 test 5 would have passed for the wrong reason. | T-051 now extends `django_db_setup` with the six-table portal grant. |
| **C7** | **`<slug>-portal.<domain>` does not work with `slug_from_host` unmodified** — it returns `labels[0]`, i.e. the literal `"acme-portal"`, which matches no tenant. The plan asserted three times that no change was needed. Also no reserved-slug validation, so a firm registering `acme-portal` hijacks `acme`'s portal host. | `portal_slug_from_host()` requires and strips the suffix; `Tenant.slug` validator rejects `*-portal`; V2 exit criteria gain `ALLOWED_HOSTS`/`DisallowedHost`. |
| **C8** | **PortalMiddleware's `MIDDLEWARE` index was unspecified, and the entire write-path safety argument depends on it.** Registered before `SessionMiddleware`, every authenticated portal request dies with `permission denied for table django_session`. Nesting inside `TenantMiddleware` is also unsafe: `SET LOCAL ROLE` **merges upward on `RELEASE SAVEPOINT`** (verified). | T-061 fixes both middlewares at index 11, exactly one active per host, with the full write-path trace and the savepoint-merge note recorded. |
| **C9** | After S9's `roles='{public}'` filter, **nothing asserted the portal policies' `WITH CHECK`** — the Phase-1 shape test stops seeing them, T-056 asserted only `qual`, and the SELECT-only grant makes `WITH CHECK` unexercisable at runtime (the privilege check fires first). It would have shipped completely unverified, and a `WITH CHECK (true)` typo is a cross-client write hole the moment 2b widens the grant. | T-056 applies the same assertions to `with_check`; T-064 adds a `WITH CHECK → true` mutation (static, so the grant does not mask it). |

Advisories A1–A7 all adopted: vacuity guard plus explicit table count (14 test / 13 dev);
`collect_sql` skip and `model_name` signature for T-052; `_client_id_of` in T-060; the
T-059→T-060 ordering stated; S9's weakening recorded as deliberate; T-051's negative
privilege assertion paired with a positive one; `CSRF_TRUSTED_ORIGINS` named as a 2b task.

**Estimate 6–9 → 7–10 days.**

### What I got wrong this round

- **C5** — I wrote "bounded by the SELECT-only grant" without checking what the grant covered. It was `ON ALL TABLES`. The justification was false as written, and I had already been asked directly whether this was a real fix or a relabelled finding. It was the latter.
- **C2** — I wrote a falsification test for the most dangerous finding in the plan, and it could not fail. That is precisely the defect class this project keeps hitting.
- **C1** — I asserted a PostgreSQL function that does not exist.

---

## Round 3 — Momus OKAY, Oracle REJECT (4 blocking, all narrow)

Oracle re-ran the PostgreSQL claims on the live cluster and confirmed C1, C2, C5, C6, C7 and
C9 behave exactly as revision 3 describes. It also verified the six-table allow-list is
**exactly sufficient** for the T-063 render path. Four defects remained.

| # | Finding | Fix in revision 4 |
| --- | --- | --- |
| **B-1** | **`TenantMiddleware` is at index 12, not 11.** Index 11 is `MFAEnforcementMiddleware`. I wrote "both at index 11 (`base.py:92`)" — line 92 *is* TenantMiddleware, so the todo contradicted itself, and the number was the load-bearing half. At literal index 11, `_must_enrol` runs **inside** the portal transaction and hits `tenants_membership` and `mfa_authenticator`, both denied. Verified: `permission denied for table tenants_membership`. **The fix for C8 reintroduced C8.** | Position now specified **structurally** — immediately after `MFAEnforcementMiddleware`, immediately before `RateLimitMiddleware` — with an acceptance assertion that the two indices are equal. Numbers drift; structure does not. |
| **B-2** | `AdminTenantMiddleware` (index 14) sits **inside** the transaction and gates on `request.path_info`, **not** `request.urlconf` — so T-062's urlconf swap does not skip it. `GET /admin/` on a portal host raises `permission denied for table tenants_tenant`: an unhandled 500 where the firm host returns 404. Nothing in T-063 exercised it, so it would have shipped silently. | `PortalMiddleware` returns 404 for the admin prefix **before** `SET LOCAL ROLE`; T-061 asserts 404-not-500. |
| **B-3** | **C5's allow-list falsified T-055's own acceptance criteria.** "Reads 0 rows from `audit_event` / `clients_tag`" became `permission denied` the moment SELECT was withheld. I had made exactly this correction for T-057 test 5 and failed to propagate it. | T-055 restated: raises `permission denied` in 2a; the `DenyPortalAccess` policies are the **2b backstop**, asserted statically by T-056 and never exercised at runtime. |
| **B-4** | **The widened guard still could not see a site T-059 itself fixes.** `Membership\.objects\.` is case-sensitive and misses `membership.objects.filter(` — the `django_apps.get_model(...)` + lowercase-local idiom at `apps/core/access.py:48`. Two of the eight call sites use it. Third version of this guard, third blind spot. | Pattern now `(?i)\b(membership\|membership_model)\.objects\.(filter\|for_user\|get_or_create\|exclude)\(`, plus a **guard self-test** asserting it matches `apps/core/access.py:48`. |

Advisories adopted: dev count is **13** (confirmed live) and `test_rls_coverage.py:166` is a
non-empty guard rather than a count assertion; `core_tests_exampletenantmodel` goes in
`PORTAL_DECISION_EXEMPT`; portal templates may not use `{{ perms.* }}` or reach
`tenants_tenant` / `mfa_authenticator` / `clients_onboardingitemtemplate`; `core.E0xx` →
**`core.E008`**; `Tenant.slug` capped at **56** chars so `<slug>-portal` stays a valid DNS
label. Estimate holds at 7–10 days.

### What I got wrong this round

- **B-1** is the one that matters. I wrote a fix whose entire purpose was preventing
  `permission denied` inside the portal transaction, and specified it with an index that
  causes exactly that. I cited `base.py:92` — correct — and then wrote "index 11" beside it
  without counting. Verified independently before accepting: `TenantMiddleware` is index 12.
- **B-3** and **B-4** are both *propagation* failures: I made the right correction in one
  place (T-057 test 5; the `.for_user(` sites) and did not carry it to the sibling that
  needed the same change.

---

## Round 4 — Momus OKAY, Oracle REJECT (4 blocking, 3 advisory)

Oracle confirmed **all four round-3 fixes are correct and complete**, including that B-3's
propagation was total (every remaining "0 rows" claim is legitimate — those tables *are*
granted). Four new defects, three of which are the same propagation shape.

| # | Finding | Fix in revision 5 |
| --- | --- | --- |
| **BL-1** | `MIDDLEWARE.index(Portal) == MIDDLEWARE.index(Tenant)` is **impossible** — two distinct list entries never share an index. "Both immediately after `MFAEnforcementMiddleware`" is equally unsatisfiable. Revision 4 replaced a wrong *number* with an unimplementable *structure*, and the load-bearing spec still could not be executed. | Concrete four-way positional assertion anchored on `MFAEnforcementMiddleware`: `[i+1]` Portal, `[i+2]` Tenant, `[i+3]` RateLimit. Robust to future insertions, and actually satisfiable. |
| **BL-2** | **`TenantMiddleware`'s "no-op" was unspecified, unowned, and its default is a silent data bug.** Today an unresolvable host still reaches `_run_in_tenant_context(request, None)` → `_apply_guc(None)`. With Portal outer, that block is a **savepoint**, and Oracle reproduced the consequence live: `set_config(..., true)` merges upward on `RELEASE`, so `app.tenant_id` reads **empty** afterwards. The Phase-1 policy then returns **zero rows silently** — worse than a 500, because nothing fails loudly. | The no-op is now specified as the first statement of `TenantMiddleware.__call__`, explicitly **not** `_run_without_database_context` (which would clobber `current_tenant_id`), assigned to T-061, with a regression test asserting one `BEGIN` and a still-set `app.tenant_id`. |
| **BL-3** | **T-051's GRANT aborts `docker compose up` and every CI run.** Reproduced: `ERROR: relation "clients_clientcompany" does not exist`. `roles.sql` runs from the init hook against an **empty** data directory under `ON_ERROR_STOP=1`. I had stated that exact fact and propagated it to the *test* DB — then wrote into `roles.sql` a form the same fact makes fatal, and never granted the dev/staging database at all. | Named grants wrapped in the `to_regclass` guard already used in `conftest.py`, **plus** an explicit post-`migrate` grant step for dev/staging folded into V1. Acceptance now includes `down -v && up` succeeding. |
| **BL-4** | `core.E007` enforces that both audit middlewares precede `TenantMiddleware` — the exact ordering T-061's write-path argument rests on — but keys on the literal class, so `PortalMiddleware` is invisible to E005/E006/E007. | Generalised over `TRANSACTION_MIDDLEWARE = (TENANT, PORTAL)` using `min()` of present indices, reusing the existing ids. |

Advisories: **AD-1** the `model_name` correction reached T-052's prose but not its own
signature or the T-053/T-054 call sites (raw table strings → `LookupError`), and the kwarg
should be `tenant_field` to match `EnableRLS`; **AD-2** four places still said "four
call-site fixes" against a six-row table; **AD-3** T-057 must call `assert_isolated_role()`
like every other isolation module, or a `SET ROLE` leak lets it pass as `app_test`
(BYPASSRLS).

### The pattern, named

Oracle's closing observation: **three of four blockers were propagation failures of an
identical shape** — an insight stated correctly in one location and not carried to the
sibling the plan's own design creates:

- empty-cluster grants → propagated to `conftest`, not to `roles.sql`'s own named grants or to dev/staging
- savepoint merge → applied to "Portal inside Tenant", not to the reverse the layout produces
- middleware-order invariant → stated for `TenantMiddleware`, not extended to `PortalMiddleware`

Before revision 5 I ran the mechanical sweep it suggested — for each verified mechanism the
plan cites, grep every *other* place that mechanism now applies. It found AD-1, AD-2 and AD-3
independently, and confirmed B-3's "0 rows" propagation was already complete. That sweep is
now the standing check before any future revision.

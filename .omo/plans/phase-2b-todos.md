# Phase 2b — executable todos

Decomposed from `phase-2b-portal-write.md` revision 9. Numbering continues Phase 2a,
which ended at T-065. Operating rules are unchanged: every commit leaves the tree green,
every todo writes QA stdout to `.evidence/<id>-{happy,failure}.txt` — **both files** —
and any mutation that changes nothing is a defect.

**Read the plan for the reasoning.** This file is the work order; it does not restate why.

## Status

| Stage | Contents | State |
| --- | --- | --- |
| 1 | C1 stash + `core.E010` | **DONE** — `98e2d43`, `c5df2b3`, `6caa057`, `217471b`, `3fcb08f` |
| 2 | V3, V4, V5, V6 | **DONE** — all four closed; V6 also implemented and deployed |
| 3 | Storage, schema, RLS, FKs, C5, pin bumps — **write grant still zero** | T-066 … T-072 |
| 4 | The write grant and the W1–W5, W7, W8 runtime proofs | T-073 … T-078 |
| 5 | Upload, download, W6 | T-079 … T-081 |

**Blocked on the operator, and Stage 3 cannot start without it**: a private OCI Object
Storage bucket and a **Customer Secret Key** (an access-key/secret pair — *not* an OCI
API signing key), plus the namespace endpoint and region.

---

## Stage 3 — everything but the grant

### T-066 — Object storage backend, with dev and tests left on the filesystem

**Do** — Add `django-storages[s3]` and `boto3`. Point `prod.py`'s `STORAGES["default"]`
at the S3 backend against OCI's S3-compatible endpoint; leave `base.py`, `dev.py` and
`test.py` on `FileSystemStorage`. Credentials come from the environment, never git.

**Acceptance** — The bucket is private, proven by a PAIR: an authenticated write-read
of a key **succeeds**, and the same key unauthenticated returns 403/404 **while the object
demonstrably exists**. *[The unauthenticated refusal alone is decoration — OCI conflates
"absent" with "unauthorised" so it cannot leak existence, so a 404 is identical for a
private bucket, a missing bucket and a wrong credential. All three occurred during this
todo.]* `storage.url()` is never called on a portal path. No public media route
exists, asserted by `/media/<anything>` returning 404 on **both** hosts.

**QA — happy**: `manage.py check` plus the private-bucket probe against the real bucket
→ `.evidence/T-066-happy.txt`
**QA — failure**: make the bucket public, assert the probe fails, revert →
`.evidence/T-066-failure.txt`
**Commit**: `feat(storage): private object storage for production documents`

> The tested backend is not the shipped backend. That is the shape operating rule 5
> exists for, so W6's assertions are written against the **view's** refusal, which is
> backend-independent, never against storage behaviour.

### T-067 — C5: document downloads reach the Marco Civil access log

**Do** — Remove the blanket `MEDIA_URL` entry from `ACCESS_LOG_EXEMPT_PREFIXES`, or
confirm the download route does not sit beneath it. Assert `/media/<anything>` 404s on
both hosts.

**Acceptance** — A download produces an access-log row. `/media/` serves nothing.

**QA — happy**: download, then assert the log row → `.evidence/T-067-happy.txt`
**QA — failure**: re-add the exemption, assert the log row disappears, revert →
`.evidence/T-067-failure.txt`
**Commit**: `fix(audit): stop exempting document downloads from the access log`

### T-068 — The documents table

**Do** — A `TenantScopedModel` with **`client_id NOT NULL`**, a UUIDv7 pk through
`apps.core.identifiers.uuid7`, a **random** `storage_key` carrying no authorisation
meaning, `sha256`, and the obligation FK the upload path attaches to.

**Acceptance** — `client_id` is `NOT NULL` at the database level. `makemigrations
--check` is clean.

**QA — happy**: migrate, inspect the column definitions → `.evidence/T-068-happy.txt`
**QA — failure**: drop the `NOT NULL`, assert an INSERT with a NULL `client_id` is
accepted where it must be refused, revert → `.evidence/T-068-failure.txt` *[corrected:
this leg previously asserted against T-070's composite FK, which does not exist yet at
T-068 time. The mutation has to be runnable when the todo runs, and `NOT NULL` is
falsifiable on its own terms — T-070 proves the FK pairing separately.]*
**Commit**: `feat(documents): the vault table, client-scoped and not-null`

> `client_id NOT NULL` is load-bearing, not hygiene: PostgreSQL's default `MATCH SIMPLE`
> **skips the composite FK check entirely** when any key column is NULL, so a nullable
> column leaves T-070's constraint present and doing nothing.

### T-069 — Portal RLS on the documents table, and W9

**Do** — `EnablePortalClientRLS` on the new table. Extend **W9**: every table the portal
can `SELECT` is client-confined on its `USING` predicate, asserted by
`test_every_tenant_table_has_a_deliberate_portal_decision`.

**Acceptance** — The new table appears in the meta-test's enumeration and passes both
the `USING` and `WITH CHECK` assertions.

**QA — happy**: the coverage meta-test → `.evidence/T-069-happy.txt`
**QA — failure**: write the policy without the `app.client_id` anchor, assert the
meta-test reddens, revert → `.evidence/T-069-failure.txt`
**Commit**: `feat(documents): client-scoped row security on the vault table`

> This is the property that retired C2. It held for the six existing tables; this todo is
> where it either extends to the seventh or the retirement argument stops being true.

### T-070 — `add_client_composite_fk()`

**Do** — Add the helper beside `add_tenant_composite_fk`, pairing on `client_id`, and
apply it to every FK on the documents table. State whether the tenant-paired FK is
dropped or kept alongside.

**Acceptance** — A document cannot reference another client's obligation. Reproduced as
`app_portal`: the cross-client insert raises `23503`, the own-client insert succeeds.

**QA — happy**: both legs, as `app_portal` → `.evidence/T-070-happy.txt`
**QA — failure**: revert to the tenant-only pairing, assert the cross-client insert
succeeds, restore → `.evidence/T-070-failure.txt`
**Commit**: `feat(core): pair portal foreign keys on client_id`

### T-071 — C4: unique constraints that leak nothing

**Do** — Every unique constraint on a portal-writable table leads with `client_id`. No
content-hash uniqueness across clients — scope it `(tenant_id, client_id, sha256)`.
`app_portal` receives **no sequence privilege**.

**Acceptance** — A collision on another client's row is not observable. The three
existence oracles are closed.

**QA — happy**: the oracle probes, all refused → `.evidence/T-071-happy.txt`
**QA — failure**: widen one constraint to `(tenant_id, sha256)`, assert the oracle
reopens, revert → `.evidence/T-071-failure.txt`
**Commit**: `feat(documents): client-leading unique constraints`

### T-072 — The predicted pin bumps

**Do** — `EXPECTED_TENANT_TABLE_COUNT` 14 → 15. `PORTAL_TABLES` 6 → 7. Add the `COUNTS`
row in `tests/isolation/test_portal_client_isolation.py`. T-064 row 9 becomes "an
eighth".

**Acceptance** — Every red is one this list predicted. **A red that was not predicted is
a stop-and-report, never a relaxation.**

**QA — happy**: the isolation suite → `.evidence/T-072-happy.txt`
**QA — failure**: bump the count without adding the table, assert the meta-test
reddens, revert → `.evidence/T-072-failure.txt`
**Commit**: `test(isolation): bump the portal table pins for the vault`

---

## Stage 4 — the grant, and proving isolation under it

### T-073 — The write grant

**Do** — A second `DO` block in `ops/sql/roles.sql` granting **`INSERT` only**, per table
and per verb, to a pinned allow-list. **W3**: the extractor uses `re.finditer` keyed on
the loop variable and **asserts both blocks were found**.

**Acceptance** — The grant runs as the table owner and is verified **by effect**
(`has_table_privilege` after), because `GRANT` without grant option is a `WARNING`, not
an error.

**QA — happy**: grant, then the privilege sweep → `.evidence/T-073-happy.txt`
**QA — failure**: issue the grant as `app_runtime`, assert it reports success and grants
nothing, revert → `.evidence/T-073-failure.txt`
**Commit**: `feat(db): grant the portal role insert on the vault allow-list`

### T-074 — W4: flat zero everywhere else

**Do** — `DELETE`, `TRUNCATE`, `REFERENCES`, **`UPDATE`** and sequence privileges
asserted flat zero at **table** level via `has_table_privilege`, in an assertion
**structurally separate** from the allow-list. Column-level `UPDATE` pinned separately
via `has_any_column_privilege`.

**Acceptance** — A blanket `GRANT UPDATE` reddens the suite.

**QA — happy**: the privilege sweep → `.evidence/T-074-happy.txt`
**QA — failure**: `GRANT UPDATE` on a vault table, assert red; then `GRANT UPDATE
(description)` and assert flat-zero still passes while the column pin sees it →
`.evidence/T-074-failure.txt`
**Commit**: `test(db): pin the portal role to insert and nothing else`

### T-075 — W1, W1b, W5: the write refusal, with a positive control

**Do** — For every allow-listed table, an INSERT with a forged `client_id` raises with
**`"violates row-level security policy"` and the named policy** in the message, and
**`"permission denied"` NOT in it**. Plus the positive control: the same INSERT with the
correct `client_id` succeeds. `STATIC_ONLY_TABLES` is an **independent literal**,
asserted equal to `PORTAL_POLICED_TABLES - WRITE_ALLOWLIST`.

**Acceptance** — Both failures are SQLSTATE `42501` and both surface as
`ProgrammingError`, so asserting on exception type alone would pass against the very
failure this exists to detect. Assert on the **message**.

**QA — happy**: forged and correct, both legs → `.evidence/T-075-happy.txt`
**QA — failure**: revoke the grant instead of forging the id and assert the test still
reddens — for the right reason — then restore → `.evidence/T-075-failure.txt`
**Commit**: `test(isolation): prove the portal write refusal at runtime`

### T-076 — W2: the deny backstop, without a permanent grant

**Do** — Inside `transaction.atomic()`, as the table owner: `GRANT SELECT`, assert the
portal reads **zero** rows, **drop the deny policy and assert the same query returns
rows**, then `ROLLBACK`.

**Acceptance** — The positive control is what makes the zero mean anything: `USING
(false)` returns zero rows and never raises, so the naive assertion passes when the grant
is absent, when the policy is absent, and when the table is empty.

**QA — happy**: all three legs → `.evidence/T-076-happy.txt`
**QA — failure**: delete the positive control, assert the test passes against an empty
table, restore → `.evidence/T-076-failure.txt`
**Commit**: `test(isolation): exercise the deny backstop transactionally`

### T-077 — W7: FK coverage

**Do** — Every FK column on an allow-listed table is either covered by a constraint whose
key includes `client_id`, **or named in a pinned exemption literal with a
justification**, shaped like `PORTAL_DECISION_EXEMPT`. The list must name
`client_id → clients_clientcompany` up front — the parent has no `client_id` because its
**pk is** the client identity, so the correct form is the tenant-paired
`FOREIGN KEY (tenant_id, client_id) REFERENCES clients_clientcompany (tenant_id, id)`.

**QA — happy**: the coverage assertion → `.evidence/T-077-happy.txt`
**QA — failure**: add an unpaired FK, assert red, revert → `.evidence/T-077-failure.txt`
**Commit**: `test(isolation): assert every portal foreign key is client-paired`

### T-078 — W8: the silent-conflict guard

**Do** — Grep guard banning `ON CONFLICT`, `ignore_conflicts=True` and `get_or_create` on
portal write paths, **with the self-test the guard family requires**.

**Acceptance** — `ON CONFLICT DO NOTHING` returns `INSERT 0 0` with no error at all, so
this is the one oracle that fails silently.

**QA — happy**: guard silent on a clean tree → `.evidence/T-078-happy.txt`
**QA — failure**: add `get_or_create` to a portal path, assert the guard fires, revert →
`.evidence/T-078-failure.txt`
**Commit**: `test(portal): ban silent conflict resolution on write paths`

---

## Stage 5 — the vault

### T-079 — Upload

**Do** — A portal view gated `@require_can("documents.transfer")` — `FULL` for both
client roles, so `core.E010` passes. Cap the ingest at `PORTAL_UPLOAD_MAX_BYTES`
(10 MiB). Attach to an obligation, which C3 has made client-paired.

**Acceptance** — The document is attributed to the uploader's client and no other. One
byte over the cap is refused cleanly, never a `MemoryError`.

**QA — happy**: upload at the cap → `.evidence/T-079-happy.txt`
**QA — failure**: one byte over, and a forged `client_id`, both refused →
`.evidence/T-079-failure.txt`
**Commit**: `feat(portal): document upload, capped and client-scoped`

### T-080 — Download, and W6

**Do** — Fetch into memory under `PORTAL_DOCUMENT_MAX_BYTES` and return an in-memory
`HttpResponse` — **never** a `StreamingHttpResponse`, which `_reject_streaming` refuses.
The size is read from object metadata **before the body is requested**.

**Acceptance** — **W6**: client A's storage key replayed as client B is refused **before
any byte is read**, by the `Document.objects.get(...)` lookup returning no row under RLS,
**asserted by spying on the storage client to prove it is never invoked**. Positive
control: B's own key succeeds in the same test.

**QA — happy**: own key succeeds, cap enforced → `.evidence/T-080-happy.txt`
**QA — failure**: replayed key refused with the storage client never called; and one byte
over the cap refused → `.evidence/T-080-failure.txt`
**Commit**: `feat(portal): document download, authorised before any byte`

> The cap must bound the **fetch**, not only the response. Under `FileSystemStorage`
> `open()` is lazy, so "before any byte is read" is true no matter what the code does —
> the assertion has to be that the client was never invoked, or it cannot fail.

### T-081 — Portal templates render under the portal role

**Do** — A test rendering **every** Stage 5 portal template under `app_portal`.

**Acceptance** — No template reaches `granted_levels`, `resolve_level` or the `{% can %}`
tag outside the stash. `portal/base.html` is standalone today, so this is latent rather
than live — which is exactly why it is written before the templates exist.

**QA — happy**: every template renders → `.evidence/T-081-happy.txt`
**QA — failure**: use the firm `base.html` in a portal template, assert the
`ProgrammingError`, revert → `.evidence/T-081-failure.txt`
**Commit**: `test(portal): render every portal template under the portal role`

---

## Follow-up design items — not Phase 2b work

### D-001 — `WARNING_AT` is the discretionary value and is the one that is hardcoded

**DECIDED for Phase 2b: keep `WARNING_AT` at `0.80` and do NOT make it configurable.**
Recorded because the split is backwards and will look like an oversight later.

`apps/obligations/threshold.py` holds two module constants and reads three dated fiscal
parameters:

| Value | Where | Changeable without a deploy | Discretionary? |
| --- | --- | --- | --- |
| `mei.annual_ceiling` | fiscal parameter, `valid_from`, per `mei_category` | yes | no — statutory |
| `mei.excess_tolerance_pct` | fiscal parameter, same | yes | no — statutory |
| `CEILING_AT = 1.00` | module constant | no | no — it IS the definition of the ceiling |
| `WARNING_AT = 0.80` | module constant | **no** | **yes — a lead-time choice** |

So the two statutory numbers are data and the single judgement call is code. `CEILING_AT`
is correctly a constant. `WARNING_AT` is the one with a defensible argument for becoming a
parameter, and it is the one that cannot move without a release.

The argument for leaving it alone: 0.80 under even revenue is crossed around month 9.6,
leaving roughly a quarter to act, and a queue whose entry point is tuned loose stops being
read at all. The argument for changing it: it is a firm-level operating preference wearing
the shape of a fiscal constant.

**If it is ever made configurable it must be a dated fiscal parameter like the other
three, never a Django setting** — a setting cannot express "this threshold changed on
1 January" and would silently reinterpret prior years.

### D-002 — `obligations.view_revenue_threshold_queue`, a collection capability

**DONE.** The revenue-threshold queue was gated on `reports.view_financial`, which is
`Limited` for `operations_admin`; `require_can` passes no object, so
`_is_attached_to(user, None)` returned False and the view was an unconditional 403 for
that role — invisibly, because `visible_nav_items` draws only `full` and the link never
rendered.

A **collection** view has no object for a refined level to be evaluated against, so the
fix is a capability of the right shape, not a laxer one. `reports.view_financial`,
`clients.view_all`, `_is_attached_to` and `portfolio_scope` are all untouched.

| role | level |
| --- | --- |
| platform_admin / owner / staff_accountant / operations_admin | `full` |
| client_owner / client_collaborator | `none` |

`clients.view_all` was rejected as the gate on two grounds: its published semantics
authorize seeing *which clients exist*, not their revenue figures; and it is the ceiling
input to `portfolio_scope`, so reusing it would collapse the authorization gate into the
queryset scoping. Scoping remains an independent layer — the gate decides 200 or 403, and
`visible_clients()` decides which rows appear.

Evidence: `.evidence/T-002-{happy,failure}.txt`.

## Out of scope, restated so it is not rediscovered

Web push, WhatsApp, enabling RLS on the five deliberately-unpoliced tables, changing any
existing RLS **policy**, direct-to-storage upload, pre-signed URLs, portal `DELETE`
(erasure routes through the existing `DataSubjectRequest` flow), and **email
notification** — cut by V5, and returning only behind a transactional outbox and a Celery
retry path.

**`audit_datasubjectrequest` cannot satisfy W1** — its RLS is disabled by design, because
a statutory request must be accepted before any tenant context exists. Either give it a
portal INSERT policy that coexists with unauthenticated intake, or keep it off the write
allow-list and route portal erasure through a firm-side endpoint. Granting INSERT on an
unpoliced table is the one option ruled out.

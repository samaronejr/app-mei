# Phase 2b — Portal write access and the document vault — Work Plan

> **Revision 6.** Revisions 1, 2 and 3 were each rejected by both reviewers. Revision 3's
> C1 call site was confirmed **correct** — both reviewers reproduced it on the live cluster,
> including the dual-membership shape. It was rejected on the *consequences* of C2, which
> revision 3 decided and did not trace. Corrections are marked **[R1→R2]**, **[R2→R3]** and
> **[R3→R4]** at the point of change.
>
> **The two round-3 reviewers disagreed, and the disagreement is the most important content
> of this revision.** Both found that `require_can` is unusable at `LIMITED`. One proposed
> passing **`request.client`** as the fix and measured `can(...) == True`. The other identified
> that same object as a **tautology**. The second is right, and it is settled in code:
> `middleware.py:243` reads `client_id = membership.client_id`, `:246` sets
> `current_client_id` from it, and `:256` sets `request.client = membership.client` — **both
> sides of `_is_attached_to` derive from one `membership` row**. The comparison cannot return
> False. The measured `True` is evidence *for* the objection, not against it.
>
> Recorded because the failure generalises: a passing measurement is not a working control.
> Operating rule 3 forbids exactly this, and the fix that a review hands you gets adopted
> with less scrutiny than the defect it replaces.
>
> The lesson is recorded here rather than in a commit message, because it generalises: I
> flagged C1 as the thing I could not verify about my own work, and I was right to flag it
> and wrong in the fix. A correction written confidently into a plan is not safer than the
> defect it replaces — it is more dangerous, because it reads as settled.
>
> **Revision 5 closes V4, V5 and V6.** V6 is not merely decided — it is **implemented in this
> change** (process-local UUIDv7 lock + `gthread` retained), so the gate is closed by effect.
> V4 chose private OCI Object Storage over the S3 API with `FileSystemStorage` kept for dev and
> tests; V5 cut the email notification from this phase. **Revision 6 closes V3 — every gate is now
> closed.** Additions are marked **[R4→R5]** and **[R5→R6]**.
>
> **Status: revised, NOT re-reviewed.** Round-3 reviewers also misreported the suite in
> opposite directions (one "every test errors", one "294 green"). Measured: `tests/portal
> tests/authz` = **207 passed**, `tests/authz/test_matrix.py` = **103 passed**. The tree is
> green; the "103 errors" was one reviewer's own probe package breaking its collection.

## What this phase is actually about

Phase 2a built the isolation layer and stopped at a page that renders one client's name. The
grant behind it is **SELECT on six tables and no write privilege anywhere**, and that single
fact is load-bearing in a way that is easy to miss:

**Verified on the live cluster.** The privilege check fires in `ExecutorStart`, before
`ExecWithCheckOptions`:

```
SELECT-only     INSERT -> ERROR 42501: permission denied for table zz_probe
+GRANT INSERT   INSERT -> ERROR 42501: new row violates row-level security policy
                          "zz_probe_portal_client_isolation" for table "zz_probe"
```

So the **six client-scoped `WITH CHECK` clauses are dead code today**, and widening the
grant is what animates them. *[R1→R2: revision 1 said "every `WITH CHECK` clause" and
counted eight. The two `DenyPortalAccess` policies carry `WITH CHECK (false)` and stay
unexercisable in 2b, because 2b grants those tables nothing. Six, not eight.]*

**But the database is not where this phase is hardest.** A document vault stores bytes, and
**PostgreSQL RLS protects rows**. Every control Phase 2a built — the role, the GUC, eight
policies, a coverage meta-test, an 18-row mutation matrix — stops at the row boundary. For
the object bytes there is exactly one layer, and it is a Django view.

> **State it plainly, because it is the opposite of the database story and that is how it
> gets forgotten: for the object bytes, the isolation boundary is one Django view. There is
> no second layer.**

## Verify-first gates — resolve BEFORE the todos they block

*[R1→R2: revision 1 had no gates. Phase 2a's V1 and V2 were both discovered in Wave 4 of its
revision 1 — "which is where plans go to fail" — and both were hard blockers. These four are
the same shape.]*

### V3 — How does a portal user receive bytes? — **CLOSED**

`PortalMiddleware._reject_streaming` raises `TypeError` for any `StreamingHttpResponse` on an
authenticated portal request, and **`django.http.FileResponse` is a `StreamingHttpResponse`
subclass**. `FileResponse(...)`, `FileResponse(storage.open(...))` and
`django.views.static.serve` all 500 on the portal host. T-061 made that an acceptance
criterion and its error message even says *"Build the payload in memory and return an
HttpResponse instead."*

Four ways out were on the table. **V4 eliminated two of them and the plan had already
rejected a third** *[R5→R6]*:

| Option | Status |
| --- | --- |
| Buffer into memory, capped | **CHOSEN** — the only survivor |
| `@non_atomic_requests` on the download view | Rejected before V4: `_run_without_database_context` runs it with **no role, no GUCs, no RLS**, moving authorisation wholly into application code — the exact failure mode 2a exists to prevent |
| `X-Accel-Redirect` / `X-Sendfile` | Rejected by V4's "no direct-to-storage"; `ops/Caddyfile` also has no internal route |
| Pre-signed URLs | Rejected by V4 explicitly, and by C4: a signed URL outlives the session that minted it and cannot be revoked inside its lifetime |

> **DECIDED — fetch the object into memory under a hard cap and return an in-memory
> `HttpResponse`.** The cap is `PORTAL_DOCUMENT_MAX_BYTES = 10 MiB`. Arithmetic, because the
> box is the binding constraint: 4 concurrent request slots (gthread 2x2) x 10 MiB = 40 MiB
> worst case on 954 MiB, roughly 4%. A MEI's DAS PDF is ~100 KB, so the cap carries about 100x
> headroom over the real workload and exists to bound the pathological case, not the normal one.

**Exit criteria — all three checkable, and criterion 3 is now resolved rather than
conditional** *[R5→R6]*:

1. No portal response path returns a `StreamingHttpResponse` subclass, asserted **by exercise,
   not by inspection**: a test walks the portal urlconf, issues an authenticated request to
   every route, and asserts `not isinstance(response, StreamingHttpResponse)`. *[R5→R6: revision
   3 said "walks the portal urlconf's view returns", and a reviewer correctly flagged that a
   return type is not statically knowable without invoking or trusting annotations. Exercising
   the route is what makes this falsifiable; `_reject_streaming` stays as the runtime backstop,
   and a test that only asserts the backstop exists would pass with every view broken.]*
2. The cap is the **named constant above**, with a boundary test: exactly at the cap succeeds,
   one byte over is refused. The refusal is a clean response, never a `MemoryError`.
3. **The chosen option does not bypass the portal transaction**, so no compensating
   authorisation is required and none is invented. Stated positively so the todo author does
   not read a dangling conditional as an open question.
4. **The cap bounds the fetch, not only the response** *[R5→R6, from V4]*. With object storage
   the bytes cross the network first; a cap applied after the object is fully in memory bounds
   nothing that matters. Assert the size is checked against object metadata **before** the body
   is read.

### V4 — Where do the bytes live, and do they survive a deploy? — **CLOSED**

Verified against the tree:

| Fact | Evidence |
| --- | --- |
| No object storage exists | `STORAGES["default"]` is `FileSystemStorage` in `base.py` and `prod.py`. No S3, no MinIO, no `django-storages` |
| Storage is a path inside the image | `MEDIA_ROOT = BASE_DIR / "media"` |
| **Nothing persists it** | `docker-compose.prod.yml` declares five volumes; **not one is media**, and `web` mounts none |
| Greenfield | Zero `FileField` / `ImageField` in `apps/` |

So as configured, **every uploaded document is destroyed on the next container recreate** —
while this plan's own out-of-scope table forbids portal DELETE because *"a client deleting
fiscal evidence is a compliance problem"*. The deployment deletes all of it, on every deploy.

> **DECIDED — private OCI Object Storage via its S3-compatible API in production;
> `FileSystemStorage` retained in development and tests.**
>
> Phase 2b exposes **no public media route, no `storage.url()`, no pre-signed URLs, and no
> direct-to-storage access**. Downloads are authorised **against the database row before any
> object byte is read**. Storage keys are random and **carry no authorisation meaning** —
> unguessability is defence in depth, never the control.

**Consequences to carry into the todos** *[R4→R5]*:

1. **New dependencies and secrets.** `django-storages[s3]` + `boto3`, and OCI's S3 compatibility
   needs a **Customer Secret Key** (access-key/secret pair), not an OCI API signing key, plus
   the namespace endpoint and region. These are deploy secrets on the same footing as
   `DATABASE_URL` — never in git, and the bucket must be **private**, asserted by an
   anonymous GET returning 403/404 against a known key.
2. **The tested backend is not the shipped backend.** Tests and dev run `FileSystemStorage`;
   production runs S3. That is precisely the shape operating rule 5 exists for — *container
   health is not deployment evidence* — so the storage swap needs its own verify-by-effect
   step against the real bucket, and W6's assertions must be written against the **view's**
   refusal, which is backend-independent, rather than against any storage behaviour.
3. **W6 gets stronger, not weaker.** "Refused before any byte is read" now means *before the
   S3 GET is issued* — an observable network call, so the requirement is falsifiable by
   asserting the client is never invoked, instead of resting on file-open ordering.
4. **V3's byte cap is now load-bearing in a second way.** Buffering an S3 object into memory
   to build the `HttpResponse` is the download path, so the cap bounds both the response size
   and the fetch.
5. **`storage.url()` must be unreachable, not merely unused.** With S3 configured, `url()`
   silently starts returning a working direct link. Assert it is never called on a portal path.

### V5 — Does production send email at all? — **CLOSED**

`EMAIL_BACKEND` is set in `dev.py` (console) and `test.py` (locmem) and **nowhere in
`prod.py`**, so production falls back to Django's SMTP default on `localhost:25`, and
`docker-compose.prod.yml` runs no MTA. Scope item 4 will silently not deliver, or 500.

> **DECIDED — new-document email notification is CUT from Phase 2b.**
>
> Production email remains a **separate pre-launch gate**, because verification, invitations
> and LGPD intake already depend on it and none of them is a 2b concern. It will use **OCI
> Email Delivery through Django's SMTP backend**, with credentials, an approved sender and
> DKIM. Document notifications return **only** behind a transactional outbox and a Celery
> retry path.

**Consequences** *[R4→R5]*: scope item 5 is struck below. Nothing in 2b may call `send_mail`
or `EmailMessage`. The outbox requirement is not decoration — a notification written inside
the portal transaction that fails on send would either roll back a committed upload or leak a
send on rollback, which is the entire reason it is deferred rather than bolted on.

### V6 — Gunicorn worker class and the UUIDv7 counter — **CLOSED AND IMPLEMENTED**

`docker-compose.prod.yml:182` ran `--workers 2 --threads 2 --worker-class gthread` while
`ops/README.md:59` required `sync --threads 1` and `apps/portal/middleware.py:34` stated it as
fact. This phase adds the first concurrent INSERT path driven by untrusted users.

> **MEASURED — the README's stated reason did not hold.** It justified `sync --threads 1`
> because `uuid6.uuid7()`'s counter is *"not thread-safe ... so two simultaneous inserts can be
> handed the same key."* The counter really is unlocked — a non-atomic read-modify-write on a
> module global, no `Lock` in the module. But **a duplicate needs the 48-bit timestamp AND the
> 76 bits from `secrets.randbits(76)` to collide** (~2⁻⁷⁶). The property at risk is **strict
> monotonic ordering, not uniqueness**. At 16 threads × 4000 generations: 0 duplicates, 0
> repeated timestamps — the race is rare, not absent, and rarity is not a guarantee.

> **DECIDED — keep `gthread`, 2 workers × 2 threads, and remove the race rather than tolerate
> it.** Every runtime UUIDv7 default routes through a project-owned **process-local lock**.
> **UUIDv7 ordering is an index-locality property, never a business ordering guarantee.**

**Implemented in this change**, so the gate is closed rather than merely decided:

| Artifact | State |
| --- | --- |
| `apps/core/identifiers.py` | new — `_LOCK` + `uuid7()` wrapper |
| `apps/core/models.py:23` | `default=uuid7` — the **only** runtime caller; all other uses are tests |
| `tests/core/test_tenant_base.py` | two pins: lock **held** during generation, field default **is** the wrapper |
| 5 `AlterField` migrations | proven no-op — `sqlmigrate` emits no DDL, the default is Python-side |
| `ops/README.md` | section retitled and the false duplicate-key rationale replaced |
| `apps/portal/middleware.py:34`, `ops/README.md` rate table, 2 test docstrings | corrected to per-thread connection reuse |
| `accounting-mei-saas.md` T-007a + T-022 | constraint and **both** acceptance criteria updated |

**The sequencing constraint is satisfied.** The decision required `sync --threads 1` until the
wrapper existed; the wrapper ships in the same change, so compose is left at `gthread` and no
interim downgrade is needed. **Production is currently running `gthread` without the wrapper**
— the exposure is the monotonicity race only, and it closes on the next deploy.

*[R4→R5: a no-duplicates test was deliberately NOT written. Unlocked generation produced none
in 64000 attempts, so such a test passes with the lock deleted — operating rule 3's decoration,
in the control added to satisfy a gate about decoration. Both pins assert the mechanism.]*

## Carried forward from Phase 2a — both were aimed at the wrong target

### C1 — `can()` cannot run inside a portal request, and it needs THREE tables

*[R1→R2: revision 1 named two tables and offered three options, two of which do not work.]*

`resolve_level` reads, in order:

1. `_capability(action)` → `authz_capability`
2. `role_of(user, tenant_id)` → **`tenants_membership`**
3. `RoleGrant.objects.filter(...)` → `authz_rolegrant`

The `permission denied for table authz_capability` error is merely the **first** to fire.
Verified: after granting the two authz tables, the next failure is
`permission denied for table tenants_membership`.

**`tenants_membership` cannot be granted.** Its `relrowsecurity` is **false** — 2a exempted
it deliberately, justified as *"the middleware reads it before `SET LOCAL ROLE`, as
`app_runtime`, so the portal never needs it. No SELECT privilege — asserted by T-056."*
Granting it hands every portal session an unfiltered **cross-tenant** read of the entire
platform's membership roster: every user, tenant, client and role, across competing firms.
That is strictly worse than the hole Phase 2a closed.

Therefore revision 1's option 1 (grant the authz tables) is **incomplete**, and option 3
(cache the matrix) is both incomplete and contrary to the design principle that the matrix is
data so changing a grant is a data edit.

**DECIDED — resolve inside `_run_in_portal_context`, after both ContextVars are live and
before the role switch.**

*[R2→R3: revision 2 said `_grant_context`. **That call site cannot work, and it fails in both
directions.**]* `role_of` does not take a client — it reads a ContextVar:

```python
# apps/authz/services.py:232
client=current_client_id.get(),
```

and the middleware's ordering is fixed:

| line | event |
| --- | --- |
| 154 | `_grant_context(...)` ← revision 2 put the call **here**, where the ContextVar is unset |
| 246 | `current_client_id.set(client_id)` |
| 256 | `request.client = membership.client` |
| 257 | `if membership is not None:` ← **the guard** *[R3→R4: omitted from revision 3's table]* |
| 258 | `_assume_portal_role()` |

At line 154 `current_client_id.get()` is `None`, so `role_of` resolves the **firm-side**
membership. Both outcomes are blocking, and reviewers reproduced both on the live cluster:

| Case | Stashed at line 154 | Truth inside the portal context |
| --- | --- | --- |
| portal-only user | every capability `NONE` | all `full` |
| **firm + client membership in one firm** | `invoices.issue: full`, `reports.view_financial: full` | `limited`, `limited` |

The first fails closed — every portal view 403s, so the phase does not ship. **The second
fails open**: the firm owner's levels are stashed onto a portal session, reopening the exact
escalation C2 exists to close, through C2's own machinery. It voids two invariants Phase 2a
shipped: `PortalMiddleware`'s `client__isnull=False`, documented as *"the SECURITY control
here"* — the stash reads `client=None`, precisely the row that filter excludes — and
`_is_attached_to`'s safety argument, *"ONLY SAFE BECAUSE `role_of` ABOVE FILTERED ON THE SAME
CLIENT"*, which becomes false when the resolution did not filter on the client at all.

**The call goes INSIDE the `if membership is not None:` guard at line 257, immediately before
`_assume_portal_role()`.** Both ContextVars are live, the role is still `app_runtime`, and line
256 already proves a tenant-scoped read is safe at that point. *[R3→R4: revision 3 said
"between line 256 and line 258", which reads literally as line 257 itself — i.e. before the
guard, unconditionally — contradicting requirement 2. Both reviewers flagged the imprecision.
It is the same class of ambiguity that produced revision 2's defect, so it is now stated as a
containment, not a range.]*

**Six requirements come with it** *[R3→R4: revision 3 shipped three. Both reviewers found the
same gap — the three cover the fall-through, the trigger and the test, and say nothing about
the stash's lifetime, whose levels it holds, or what is in it. A stash is new mutable
request state; requirements 4-6 are its contract]*:

1. **`can()` must RAISE** if asked for a capability outside the stashed set during a portal
   request — never fall through to `resolve_level` (which would hit the denied tables) and
   never return `NONE` (which would fail closed silently and be read as a permissions bug).
2. **The stash is keyed on "the portal role was assumed", not on the host.** `/accounts/`
   runs with `membership is None` and no role switch, so it must take the ordinary path.
3. **A named test for the dual-membership user** — firm-side *and* client-side membership in
   one firm. That is the shape that turns this from fail-closed into fail-open, and **nothing
   in the suite covers it today.**
4. **Lifetime: token/reset, cleared in `_run_in_portal_context`'s existing `finally`** — never
   a bare `.set()`. `middleware.py:244` already carries this warning for the two existing
   ContextVars: *"token/reset, never a bare set(): one thread serves many requests."* A
   reviewer measured a bare `.set()` at the plan's own call site surviving the response —
   `documents.transfer: full` still live on the thread after the request returned. That is a
   **cross-request** leak under **both** V6 outcomes, since `sync --threads 1` reuses one
   thread across requests exactly as gthread does. Pinned by a test asserting the stash is
   `None` after the response, and by a second portal request on the same thread as a different
   user.
5. **The stash answers only for the identity it was computed for.** `granted_levels` is
   computed once for `request.user`, but `can(user, action, obj)` accepts **any** user.
   Measured: inside one client context the stash returns the portal user's
   `documents.transfer: full` for a firm OWNER whose true level is `NONE` — a pure-Python
   fail-open, with requirement 1's "must RAISE, never fall through" removing the escape hatch.
   `can()` consults the stash **only** when `user == request.user`, and takes the ordinary path
   otherwise. Unlike the dual-membership defect this is not created by ordering, so moving the
   call site does nothing for it.
6. **The stashed set is named and pinned.** Revision 3 said `can()` must raise for a capability
   *"outside the stashed set"* and never said what the set is. The two readings are not
   equivalent: **all 16 matrix slugs** costs the same 3 queries and makes requirement 1 nearly
   dead code (`granted_levels` already raises `UnknownCapability` for an unknown slug), while a
   **curated portal list** makes requirement 1 load-bearing and turns a new portal capability
   into a **500**. Decide, and pin the set against the portal urlconf's actual `can()` call
   sites so a new gated view cannot silently fall outside it.

**Coverage** *[R3→R4]*: requirement 1 constrains `can()` only. `resolve_level` and
`granted_levels` are both exported and both read the three denied tables — called from
`apps/core/navigation.py:75` and `apps/obligations/views.py:332,364`. Not reachable from
`portal/base.html` today, which is deliberately standalone with no nav, but Stage 5 adds portal
views and templates. State whether they read the stash or are banned from portal paths;
an unstashed call dies with `ProgrammingError: permission denied for table authz_capability`.

**Two supporting claims in revision 2 were also wrong** *[R2→R3]*:

* *"costs two queries"* — it is **three**, pinned as `BULK_QUERY_BUDGET = 3` in
  `tests/authz/test_granted_levels.py:25` ("one capability existence check, one membership
  read, one grant read").
* *"already pinned to agree with `resolve_level` by a walking test"* — true in letter,
  **vacuous for the two portal roles**. `client_context` appears **zero times** in that test
  file; it runs under `tenant_context` only, so for `client_owner` and `client_collaborator`
  both sides return all-`NONE` and the parametrisation compares two empty answers. **The pin
  this decision leans on does not cover the case it is needed for.** Extending that test with
  a `client_context` parametrisation is part of this todo, not a follow-up.

### C2 — the escalation trigger is real, but the branch is not the control

*[R1→R2: revision 1 said "checking `user` is mandatory". Verified: that would change nothing.]*

`documents.transfer` is granted **`FULL` to all six roles**, including `client_owner` and
`client_collaborator` (`apps/authz/matrix.py`). And `can()` short-circuits:

```python
level = resolve_level(user, action, tenant_id=tenant_id)
if level == GrantLevel.FULL:
    return True          # <- returns BEFORE _is_attached_to is ever reached
```

So `can(portal_user, "documents.transfer", another_clients_document)` is **`True` today**,
and `_is_attached_to` — the branch whose ignored `user` argument revision 1 treated as the
finding — never executes on this path. **The grant level is the control, not the branch.**

Under `FULL`, the object dimension is skipped entirely and **RLS is the only barrier for a
portal write**. Combined with C3 below, a cross-client attachment has nothing above the
database to catch it, and the database lets it through.

**DECIDED — (a): grant portal write capabilities at `LIMITED`**, so `_is_attached_to` runs
and the object dimension is real; then add the `user` check, which makes the branch
load-bearing as revision 1 wrongly assumed it already was. Pinned by a test asserting the
*level*: `resolve_level(portal_user, "documents.transfer") == GrantLevel.LIMITED`. Today's
silent `FULL` is what made C2's whole discussion moot.

Firm-side behaviour is genuinely unchanged — the four firm columns are untouched — but the
demotion has **three consequences revision 2 did not name** *[R2→R3]*:

1. **`tests/authz/test_matrix.py` parses `deep-research-report.md`, not `matrix.py`.** Row 59
   reads `| Upload/download documents | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |`; its last two cells
   must become `Limited` or 2 of 96 parametrised cells go red. **`apps/authz/matrix.py`'s
   docstring is the pin for why that matters** — *"The matrix test does not trust this file: it
   parses the report itself... which is why an error introduced here still turns the build
   red."* *[R3→R4: revision 3 cited `pyproject.toml` as calling the report "read-only reference
   material". The phrase is real, at `:50`, but it justifies `extend-exclude = ["*.md"]` — it
   means ruff must not reformat it, and it calls the plan documents read-only too, which are
   rewritten every revision. It does not carry a governance claim; the `matrix.py` docstring
   does.]* Decide explicitly whether the report or the matrix is the
   oracle — the test's independence claim rests on them being two separate sources.
2. **A new `RunPython(seed)` migration is required.** `0002_seed_matrix` is already applied
   and Django will not re-run it. `seed()` uses `update_or_create`, so a fresh migration
   fixes existing databases — without it, a fresh test DB goes green while dev and staging
   keep `full`. That is exactly the verify-by-effect failure decided constraint 6 exists to
   prevent, reproduced inside this plan.
3. **`PORTAL_FULL_CAPABILITY = "documents.transfer"`** (`tests/authz/test_portal_authorization.py:30`)
   still *passes* after the demotion, via `_is_attached_to` — so its name, its comment
   ("no object refinement is needed") and its purpose all become false **silently**, which
   operating rule 3 forbids. Re-point it at `clients.view_assigned` and add a LIMITED
   assertion.
4. **The demotion breaks `require_can`, which is the only decorator that consumes it**
   *[R3→R4 — BLOCKING in both round-3 reviews, and unaddressed in revision 3, where the string
   `require_can` does not appear once]*. `require_can` calls `can(request.user, action)` with
   **no object** (`apps/authz/services.py:175`); at `LIMITED` that reaches
   `_is_attached_to(user, None)`, which returns `False` at `:240` before anything else runs.
   Measured end-to-end on a real portal request against a `@require_can("documents.transfer")`
   view: **`full` → 200, `limited` → 403**, unconditionally, for the client's own upload. It is
   the codebase idiom (`dashboard.py:137`, `obligations/views.py:296-352`, `accounts/views.py:73,193`,
   `clients/views.py:24`), so C2 as decided makes Stage 1 unshippable.

**DECIDED — (b): the object is the obligation, never `request.client`.**

> **`request.client` is rejected, and this is the correction that matters most in revision 4.**
> One round-3 reviewer proposed it and measured `can(...) == True`. But `request.client`
> (`middleware.py:256`) and `current_client_id` (`:246`) both come from the **same `membership`
> row** read at `:243`, so `_is_attached_to` compares `membership.client.pk` to
> `membership.client_id` — **always True, unfalsifiable**. It would make C2's stated purpose
> ("so the object dimension is real") false on the phase's primary write path, and operating
> rule 3 forbids a control that cannot fail.

* **Upload attached to an obligation** — pass the **obligation**, fetched by id from the
  request. Its `client_id` comes from a database row, independent of the membership, so the
  comparison can genuinely refuse and a test can turn it red. C3 is already making that row
  client-paired, which is what makes this sound.
* **Standalone upload with no obligation** — there is **no object with an independent
  `client_id`**, so `can()` at `LIMITED` cannot be a real control there. Say so: the controls
  on that path are the RLS `WITH CHECK` (W1) and the client-paired FK (C3), and constraint 5
  and success criterion 8 are narrowed accordingly rather than left false.
* **Mechanism** — `require_can` gains an optional resolver mapping request → object. A portal
  view gated on a `LIMITED` capability **without** a resolver must fail **loudly at startup**,
  not 403 at request time; a silent universal 403 is the failure mode requirement 1 already
  forbids for `can()`. Pinned by a parametrised `full`/`limited` test of the gated view.

**Superuser is out of scope, stated rather than left false** *[R3→R4]*: `resolve_level` and
`granted_levels` both short-circuit `is_superuser` to `FULL` before membership is consulted, so
a superuser holding a client membership passes every portal `can()` at `FULL` and **never
reaches `_is_attached_to`**. Measured: every capability `full`, including `clients.view_all`.
This is pre-existing and RLS still confines the rows, but it means criterion 8 does not hold
for that shape — and if W6's check is `can()`, a superuser passes it for any client's bytes.
Either gate the portal on `not is_superuser` or accept it explicitly here.

## New in revision 2 — findings the review reproduced

### C3 — the composite FK proves same *tenant*, not same *client*: a cross-client WRITE

`apps/core/migrations/_composite_fk.py` pairs on the tenant column only. Its own docstring
names this hazard one dimension up; 2b reopens it one dimension down. **Reproduced as
`app_portal` for client A:**

| Action | Result |
| --- | --- |
| `SELECT` client B's obligation | **0 rows** — RLS holds |
| `INSERT` a document, `client_id = A`, `obligation_id = <B's>`, FK on `(tenant_id, obligation_id)` | **`INSERT 0 1`** — succeeds |
| Same with a fabricated obligation id | `23503` — a clean existence oracle |
| Same after pairing the FK on `client_id` | `23503` for B's row, success for A's own |

RLS is not bypassed: `WITH CHECK` inspects the child's own `client_id`, which is honest. FK
checks bypass row security by design, so the *reference* is unconstrained.

**Fix**: every FK on a portal-writable table pairs on `client_id`.

```sql
ALTER TABLE <parent> ADD CONSTRAINT <p>_tcid UNIQUE (tenant_id, client_id, id);
ALTER TABLE <child>  ADD CONSTRAINT <c>_fk
  FOREIGN KEY (tenant_id, client_id, <col>)
  REFERENCES <parent> (tenant_id, client_id, id) ON DELETE CASCADE;
```

Add `add_client_composite_fk()` beside the existing helper, and assert coverage (**W7**) —
without that assertion this is one forgotten `models.ForeignKey` away from recurring.

**`client_id` must be `NOT NULL` on every portal-writable table** *[R2→R3]*. PostgreSQL's
default `MATCH SIMPLE` **skips the composite FK check entirely when any key column is NULL**,
so a nullable `client_id` leaves the client pairing silently unenforced — the constraint
exists, and does nothing. State also whether the tenant-paired FK is **dropped** or kept
alongside the client-paired one; the latter subsumes it, and carrying both is two constraints
where one is load-bearing.

### C4 — write grants create three existence oracles

All reproduced. Values do **not** leak — RLS suppresses the `DETAIL: Key (...) already
exists` line — but existence does:

| Mechanism | Observed |
| --- | --- |
| `UNIQUE (tenant_id, storage_key)` collision | `23505` vs `INSERT 0 1` — 1-bit existence oracle |
| `UNIQUE (tenant_id, sha256)` collision | proves another client holds a byte-identical document |
| `ON CONFLICT DO NOTHING` | `INSERT 0 0` vs `INSERT 0 1` — **silent**, no error at all |
| granted sequence `last_value` | portal sees 0 rows, reads a global cross-client row counter |

**Fixes**: every unique constraint on a portal-writable table leads with `client_id`; no
content-hash uniqueness across clients (scope it `(tenant_id, client_id, sha256)`); storage
key is a random UUID; **ban `ON CONFLICT` / `ignore_conflicts=True` / `get_or_create` on
portal write paths** via the grep-guard family, because that one fails silently; UUIDv7 pks
only and **`app_portal` never receives a sequence privilege**.

### C5 — document downloads are excluded from the Marco Civil access log by construction

`ACCESS_LOG_EXEMPT_PREFIXES = ["/healthz", STATIC_URL, MEDIA_URL]`. For a fiscal document
vault, downloads are exactly the events an investigation asks for. **Must be fixed whatever V3 decides.** *[R4→R5: V4 chose object storage with **no public
media route**, so the download is a portal view path and is therefore already outside the
`MEDIA_URL` exemption — C5's remaining work is the `/media/<anything>` 404 assertion on both
hosts, plus confirming the download route does not sit under `MEDIA_URL`.]* Also: nothing serves `/media/` today, which is the good news — and it is one
line (`static(settings.MEDIA_URL, ...)`, or a Caddy `file_server`) from every document being
world-readable with no session, tenant, client or policy involved. Assert `/media/<anything>`
404s on both hosts.

## Scope

### Covered

1. C1 and C2 — no schema change, no grant, unblocks every portal view.
2. C3, C4, C5 — the write-path holes the review reproduced.
3. Widening `app_portal` to the minimum writes the vault needs, and re-proving isolation
   under them at runtime for the first time.
4. A document vault: storage per V4 (private OCI Object Storage, S3 API), upload, download
   per V3. **No email notification — cut by V5.**

### Explicitly OUT of scope

| Item | Why | Phase |
| --- | --- | --- |
| Web push, service worker, PWA | iOS reach ≈1–2% | deferred |
| WhatsApp Business API | Costs money, needs numbers we do not collect | 3+ |
| Enabling RLS on the five deliberately-unpoliced tables | Reopens the Phase-1 bootstrap problem | separate |
| Changing any existing RLS **policy** | Restrictive policies compose | never |
| Direct-to-storage upload; pre-signed URLs | Removes the only place `can()` and RLS can run | 2c at the earliest |
| Portal `DELETE` | See below — **this is not "never"** | — |

**On portal DELETE** *[R1→R2: revision 1 said "never", treating a legal question as an
engineering one]*: withholding DELETE from `app_portal` is right, and LGPD Art. 16 supports
retention where a legal obligation exists — which fiscal evidence is. But a data subject has
erasure rights, and a mis-uploaded document containing personal data with no removal path is
itself an LGPD problem. **Route erasure through the existing `DataSubjectRequest` flow, which
already models `DELETION`**: the client requests, the firm actions it. `app_portal` needs
INSERT on the request table and DELETE on nothing.

## Decided constraints

1. Isolation is enforced by **DB role**, not a shared GUC. Unchanged from 2a.
2. The grant to `app_runtime` stays `WITH INHERIT FALSE, SET TRUE`; `core.E008` enforces it.
3. Writes are granted **per table and per verb**. No blanket `GRANT`. **`INSERT` only** — see
   constraint 4.
4. **`app_portal` never receives `DELETE`, `TRUNCATE`, `REFERENCES`, or any sequence
   privilege.** *[R1→R2: revision 1 named only DELETE and TRUNCATE, silently dropping
   REFERENCES from a shipped invariant — a role holding REFERENCES can create an FK to a
   table it cannot read and use FK violations as an existence oracle.]* And note `UPDATE` is
   excluded on purpose: with `UPDATE` granted, `UPDATE documents SET status='deleted'` is a
   delete in every sense this plan cares about. If metadata editing is genuinely needed, use
   a **column-level** grant — `GRANT UPDATE (description) ON ... TO app_portal`.
5. Every portal view **that has an object carrying an independent `client_id`** authorises
   through `can()` at `LIMITED`, so the object dimension runs. *[R3→R4: revision 3 said "every
   portal view", which is unsatisfiable — on a standalone upload the only available object is
   `request.client`, and that comparison is a tautology. Narrowed rather than left false]*
6. The post-`migrate` grant step **runs as the table owner and verifies by effect**.
   `GRANT` without grant option is a **`WARNING`, not an error** — verified: issuing it as
   `app_runtime` reported success and granted nothing. Assert `has_table_privilege` after.

## Verification requirements

*[R1→R2: revision 1's W1–W5 were the defect class this project hunts. W1 could not fail, W2
was self-contradictory and silent, W4 restated an existing test while narrowing it, W5
duplicated W1. All rewritten; W6–W8 are new.]*

| # | Requirement |
| --- | --- |
| **W1** | For every table on the write allow-list: an INSERT with a forged `client_id` raises with **`"violates row-level security policy"` and the named policy** in the message, **and `"permission denied"` NOT in it** — that last clause is what turns the mutation "revoke the grant instead of forging the id" red. Plus a **positive control**: the same INSERT with the correct `client_id` succeeds. Both failures are SQLSTATE `42501` and both surface as `ProgrammingError`, so asserting on exception type alone passes against the very failure W1 exists to detect |
| **W1b** | For every portal-policed table **not** on the write allow-list, `WITH CHECK` remains statically asserted. `STATIC_ONLY_TABLES` is an **independent literal list**, asserted equal to `PORTAL_POLICED_TABLES - WRITE_ALLOWLIST`. *[R2→R3: if it were DEFINED as that difference the assertion is a tautology that can never fail — the revision-1 W1 defect, reproduced in the requirement written to replace it. The literal is what makes a table dropping out of runtime coverage turn the meta-test red]* |
| **W2** | The `DenyPortalAccess` backstop is exercised **without a permanent grant**: inside `transaction.atomic()`, as the table owner, `GRANT SELECT`, assert the portal reads **zero** rows, then **drop the deny policy and assert the same query returns rows** (the positive control that makes the zero mean anything), then `ROLLBACK`. Verified: `GRANT` is transactional and leaves no residue. `USING (false)` on SELECT **returns zero rows and never raises**, so the naive assertion passes when the grant is absent, when the policy is absent, and when the table is empty |
| **W3** | The write allow-list is pinned, `ops/sql/roles.sql` remains the oracle, and the extractor uses `re.finditer` keyed on the loop variable (`portal_table` vs `portal_write_table`) and **asserts both blocks were found** — a single `re.search` returns only the first, leaving the write list pinned by nothing while every assertion stays green |
| **W4** | `DELETE`, `TRUNCATE`, `REFERENCES`, **`UPDATE`** and sequence privileges are asserted **flat zero at TABLE level** via `has_table_privilege`, in an assertion **structurally separate** from the allow-list. Column-level `UPDATE` grants are pinned separately via `has_any_column_privilege`. *[R2→R3: revision 2 dropped `UPDATE` from the flat-zero set — narrowing a shipped five-privilege invariant, so a blanket `GRANT UPDATE` would have passed every requirement in this plan. Unnecessary: verified on the cluster that after `GRANT UPDATE (description)`, `has_table_privilege(...,'UPDATE')` is **false** while `has_any_column_privilege(...,'UPDATE')` is **true** — flat-zero and the column grant constraint 4 permits are compatible]* |
| **W5** | A portal user cannot write a row attributed to another client, proven at the DB layer with a positive control. Covers `INSERT`; if any column-level `UPDATE` is granted, covers `UPDATE` separately, whose semantics differ (`USING` selects the rows, `WITH CHECK` constrains the result) |
| **W6** | A portal user issued client A's storage key, replaying it as client B, is **refused before any byte is read, by an authorisation check in the Django download view — named here, not deferred**, with a positive control (B's own key succeeds in the same test). *[R3→R4: revision 3 delegated the naming to V4, whose three exit criteria pin a backend, key randomness and non-dependence on secrecy, and name no check and no location. Both reviewers flagged the dangling reference. W6's own note already stated the answer, so the delegation was circular]* *[R2→R3: revision 2 said "at the storage layer" — a category error under V4's own recommended outcome, since `FileSystemStorage` has no layer that can refuse anything. The refusal is the Django view, which is this plan's stated single layer, and naming it that way is what makes the requirement satisfiable]* Without this, W1–W5 protect the metadata while the bytes stay open |
| **W7** | For every table on the write allow-list, every FK column is either covered by a constraint whose key includes `client_id`, **or named in a pinned exemption literal with a justification**, in the shape of `PORTAL_DECISION_EXEMPT`. **The exemption list must name `client_id → clients_clientcompany` up front** *[R3→R4]* — the parent has no `client_id` column because its **pk is** the client identity (`_client_id_of`, `services.py:269-275`), so the correct constraint is the tenant-paired `FOREIGN KEY (tenant_id, client_id) REFERENCES clients_clientcompany (tenant_id, id)`. It is the first FK a documents table meets, and unnamed it gets decided ad hoc on contact — the exact failure this requirement's escape hatch exists to prevent. *[R2→R3: revision 2's "every FK column" is unsatisfiable — `tenant_id → tenants_tenant` and `uploaded_by_id → accounts_user` can never be client-paired, because those parents have no `client_id`. Without a pinned exemption it gets relaxed ad hoc on first contact]* |
| **W8** | No `ON CONFLICT` / `ignore_conflicts` / `get_or_create` on any portal write path (grep guard, with the self-test the guard family requires) |

### Inherited pins this phase will break, deliberately

*[R2→R3: revision 2 listed two. There are seven, and an incomplete list defeats the section's
whole purpose — it exists precisely so nobody meets an unexpected red and relaxes it.]*

| Pin | Change | Stage |
| --- | --- | --- |
| `EXPECTED_TENANT_TABLE_COUNT = 14` | → **15** once the documents table carries `tenant_id` | 3 |
| `PORTAL_TABLES` 6 → **7** | T-064 row 9 ("add a 7th entry") becomes "an eighth" | 3 |
| `test_the_portal_role_holds_no_write_privilege_anywhere` | flat-zero sweep goes red the moment INSERT is granted; must split into allow-listed INSERT + flat-zero DELETE/TRUNCATE/REFERENCES/UPDATE | 4 |
| `test_the_conftest_allow_list_matches_the_production_artifact` | reshaped when `roles.sql` gains a second `DO` block | 4 |
| `test_the_portal_role_can_read_the_allow_list_and_nothing_else` | same | 4 |
| `tests/authz/test_matrix.py` | 2 of 96 cells, against the `deep-research-report.md` oracle | 1 |
| `PORTAL_FULL_CAPABILITY` | passes but becomes semantically false — re-point it | 1 |

Every red is **correct**; the pins exist to force a deliberate edit. What is not acceptable is
meeting one that this table did not predict.

## Execution stages

*[R1→R2: revision 1 asserted "the grant widening and C1/C2 come before any vault code". The
second half is circular — you cannot `GRANT INSERT` on a table the vault migration has not
created. 2a says so itself in T-051: "on a fresh cluster the guard grants nothing — the
tables do not exist yet".]*

| Stage | Contents | Gate |
| --- | --- | --- |
| 1 | **C1 + C2.** No schema change, no write grant. *[R2→R3: not "pure `apps/authz/` and middleware work" — C2 also needs a data migration and an edit to `deep-research-report.md`]* Unblocks every portal view | — |
| 2 | **No gate work remains** — V3, V4, V5 and V6 are all closed, and V6 is already implemented. Stage 2 collapses into Stage 3 | Stage 1 green |
| 3 | **C5**, documents schema, RLS policy, client-paired FKs, `client_id`-leading unique constraints, count-pin bumps — **with the write grant still zero**, so T-056 stays green throughout | V-gates closed |
| 4 | **The write grant**, the per-verb allow-list mechanism, C3/C4 guards, and the **W1–W5, W7, W8** runtime proofs. Necessarily last: they need the table to exist | Stage 3 green |
| 5 | Upload, download — **and W6**. No notification: cut by V5 | Stage 4 green |

*[R3→R4: revision 3 put "the W1–W8 runtime proofs" in Stage 4. **W6 replays a storage key
against a download view that Stage 5 creates**, so Stage 4 could not deliver it. Both reviewers
flagged it]*

## Operating rules

Unchanged from 2a, and two added because they cost real time there:

1. Every commit leaves the tree green.
2. Every todo writes QA stdout to `.evidence/<id>-{happy,failure}.txt` — **both files**.
3. **Any mutation that changes nothing is a defect.** That control is decoration.
4. **Assume any fixture convenience is hiding something.** Wave 3's fixtures pre-enrolled
   TOTP, with a comment explaining it avoided the enrolment redirect — which was exactly the
   path that 500'd for every new user.
5. **Container health is not deployment evidence.** A `--no-build` deploy restarted the app
   on the old image, all containers healthy, `manage.py check` passing, zero migrations run.
   Verify by effect.

## Success criteria

1. A portal user uploads a document; it is attributed to their client and no other.
2. A forged `client_id` on write is refused **by a named policy**, with a positive control.
3. A document cannot be attached to another client's obligation — the C3 hole, closed and
   asserted.
4. Another client's storage key, replayed, is refused **before any byte is read**, by the
   authorisation check in the Django download view named in W6.
5. The `DenyPortalAccess` policies refuse a read at runtime, proven without a permanent grant.
6. `app_portal` holds no `DELETE`, `TRUNCATE`, `REFERENCES` or sequence privilege, and write
   access only to the pinned allow-list.
7. Document downloads appear in the Marco Civil access log.
8. Every portal view holding an object with an independent `client_id` authorises through
   `can()` at `LIMITED`; the paths that do not are named, and their controls (RLS `WITH CHECK`,
   client-paired FK) are stated instead.
9. The firm still sees everything it saw before — `pg_has_role('app_runtime','app_portal',
   'USAGE')` false, firm-side row counts unchanged.

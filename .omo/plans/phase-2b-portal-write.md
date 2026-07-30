# Phase 2b — Portal write access and the document vault — Work Plan

> **Revision 7.** Revisions 1, 2 and 3 were each rejected by both reviewers. Revision 3's
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
> tests; V5 cut the email notification from this phase. **Revision 6 closed V3.** Additions are marked
> **[R4→R5]**, **[R5→R6]** and **[R6→R7]**.
>
> ## Revision 7 retires C2, because my correction was a tautology for the third time
>
> Both round-4 reviewers rejected revision 6 on the same finding, each proving it
> independently on the live cluster: **the obligation is a tautology, exactly as
> `request.client` was.**
>
> `obligations_obligation` is on the portal SELECT allow-list AND carries a RESTRICTIVE
> policy `client_id = current_setting('app.client_id')`. So the chain runs
> `membership.client_id` -> `current_client_id` -> the `app.client_id` GUC -> the RLS
> predicate -> the visible row set. **Any obligation a portal request can fetch is already
> client-matched**, so `_is_attached_to` compares the GUC against itself.
>
> Exhaustively, and this is what makes it final rather than fixable:
>
> | Case | Outcome |
> | --- | --- |
> | Attacker passes another client's obligation id | RLS returns 0 rows -> `DoesNotExist` -> **404 before `can()` is ever called** |
> | Any id that does resolve | The row is client-confined by the same GUC -> `_is_attached_to` **always True** |
>
> There is no third case, and **this generalises to every object the portal can reach**:
> all six portal-readable tables are client-confined on that one GUC. The object dimension
> cannot refuse anywhere on the portal surface. A test can only turn it red by constructing
> an object production cannot construct — fetching as `app_runtime` or `app_test` — which
> tests a pure Python function, not a control.
>
> **So C2's premise was false from the start.** It feared
> `can(portal_user, "documents.transfer", another_clients_document)` returning True. That
> object is unfetchable in a portal request. The escalation C2 was written to close cannot
> be reached, the demotion to `LIMITED` changes nothing observable, and the resolver plus
> startup check exist to enforce something unfalsifiable. By operating rule 3 all three are
> decoration, so revision 7 removes them rather than defending them.
>
> This is the third correction in this plan that reproduced the defect class it replaced —
> revision 2 fail-open, revision 4 tautology, revision 6 tautology one layer deeper. The
> pattern is that each fix was checked for *correctness* and never for *reachability*. The
> question that would have caught all three: **can this control refuse, on a path a real
> request can take?**
>
> **Status: revised, NOT re-reviewed.** Round-3 reviewers misreported the suite in
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
> `HttpResponse`.** `PORTAL_DOCUMENT_MAX_BYTES = 10 MiB` on download, and
> `PORTAL_UPLOAD_MAX_BYTES = 10 MiB` on ingest.

**The arithmetic in revision 6 was wrong, and wrong in the unsafe direction** *[R6→R7]*. It
computed 40 MiB against the **host's** 954 MiB and called it "roughly 4%". The web container is
capped at **400 MiB** (`docker-compose.prod.yml`), so the real figure is **10% of the limit that
actually kills the process** — and the declared limits across all six services already sum to
**1488 MiB on a 954 MiB box**, which is why the deployment carries a 4 GiB swapfile. The cap
survives that correction, but the headroom claim does not, and an in-memory `HttpResponse` can
hold the payload twice (fetch buffer plus response body) while gunicorn writes to a slow client.

**The upload direction had no cap at all** *[R6→R7 — both reviewers]*. Revision 6 bounded reads
and left writes open. Django's defaults spool a large body to disk without an upper bound, so an
authenticated portal user could exhaust the container's filesystem — the one direction untrusted
users fully control, on the box that is already the binding constraint. `PORTAL_UPLOAD_MAX_BYTES`
matches the download cap deliberately: **a document larger than the download cap would be stored
and then permanently unreadable**, which silently violates success criterion 1.

**Exit criteria — four, and all four falsifiable** *[R6→R7: revision 6 said "all three
checkable" while listing four, and two of the four could not fail]*:

1. **Every portal route completes without `_reject_streaming` firing, and returns a
   non-streaming response.** *[R6→R7: revision 6 asserted `not isinstance(response,
   StreamingHttpResponse)`. Both reviewers showed that assertion is unreachable — on any route
   carrying a membership, `_reject_streaming` raises `TypeError` first, so deleting the
   assertion changes nothing and the backstop does all the work. Worse, 23 of the portal
   urlconf's 25 patterns are allauth's tree under `/accounts/`, where `membership is None` and
   the backstop is skipped, and `/healthz` is `@transaction.non_atomic_requests` so it never
   enters the portal context at all. The criterion now asserts the **absence of the TypeError**,
   which is the signal that can actually change.]* The walk must skip `logout`, which destroys
   the session mid-walk and turns every later route into a redirect that asserts nothing.
2. The download cap is the **named constant**, with a boundary test: exactly at the cap
   succeeds, one byte over is refused with a clean response, never a `MemoryError`.
3. **The chosen option does not bypass the portal transaction**, so no compensating
   authorisation is required and none is invented. Stated positively so it is not read as an
   open question. Note the download now makes a **synchronous network call to object storage
   while holding the portal transaction open** — that is a latency property to measure, not a
   correctness problem.
4. **The cap bounds the fetch, not only the response**, asserted **by spying on the storage
   client**: the size is read from object metadata and the body is never requested when the cap
   is exceeded. *[R6→R7: revision 6 said "assert the size is checked before the body is read".
   Under `FileSystemStorage` — the backend the tests actually run — `open()` is lazy, so "before
   any byte is read" is true no matter what the code does, and the assertion cannot fail. Spying
   on the client is what makes it falsifiable, and it is the same technique W6 already uses.]*

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

**Consequences** *[R4→R5]*: the notification item is struck from the scope list below (which is now four items, not five) *[R6→R7: the reference said "item 5" after the renumber]*. Nothing in 2b may call `send_mail`
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

**Eight requirements come with it** *[R3→R4: revision 3 shipped three. Both reviewers found the
same gap — the three cover the fall-through, the trigger and the test, and say nothing about
the stash's lifetime, whose levels it holds, or what is in it. A stash is new mutable
request state; requirements 4-8 are its contract. Revision 7 adds two more: both round-4 reviewers found
the `tenant_id` hole independently, and one built a concrete cross-tenant escalation]*:

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
5. **The stash answers only for the identity it was computed for**, compared **by primary key
   on an authenticated user**, never a bare `==`. *[R6→R7: two `AnonymousUser` instances compare
   equal, so `==` alone matches an identity the stash was never computed for. Requirement 2
   should stop an anonymous request having a stash at all, but a contract that leans on another
   requirement to stay sound is one edit from being wrong.]* Originally: `granted_levels` is
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
7. **The stash is bound to `tenant_id`, not only to the user** *[R6→R7 — BLOCKING in both
   round-4 reviews]*. `can(user, action, obj, tenant_id=...)` takes **four** inputs.
   Requirements 1, 2 and 5 bind the trigger, the user and the capability, and leave `tenant_id`
   unbound — so a portal request calling `can(request.user, cap, tenant_id=OTHER)` is answered
   from levels computed for the **portal's** tenant. A reviewer built the escalation: a user who
   is `client_owner` in T1 and holds **no membership at all** in T2 is handed T1's `FULL` for a
   T2 question whose truth is `NONE`. Real call sites already pass an explicit `tenant_id`.
   **Consult the stash only when the user matches AND `tenant_id` is omitted or equals the
   request's tenant; otherwise take the ordinary path.**
8. **Bind the token before the `try:`, never inside it** *[R6→R7]*. The existing two tokens are
   bound at the top for exactly this reason. The stash's call site sits inside `try:`, after
   `_apply_gucs` and the lazy `membership.client` fetch — both of which can raise — so a token
   assigned there leaves `finally` raising `UnboundLocalError`, **masking the original
   exception**. Bind it to `None` beside the other two and guard the reset.

**Coverage — DECIDED, not left open** *[R6→R7: revision 4 said "state whether they read the
stash or are banned from portal paths", which is satisfiable by writing one sentence. That is
the prose-satisfiable shape operating rule 3 forbids, inside the paragraph written to close a
gap]*:

Requirement 1 constrains `can()` only. Three other entry points read the three denied tables
and would die with `ProgrammingError: permission denied for table authz_capability`:

| Entry point | Reached from | Decision |
| --- | --- | --- |
| `resolve_level` | exported, and from `can()` | Reads the stash under the same eight rules |
| `granted_levels` | `visible_nav_items` in `apps/core/navigation.py` | Reads the stash under the same eight rules |
| `{% can %}` template tag / filter | `apps/authz/templatetags/authz.py` | Routes through `can()`, so covered — **but revision 4's coverage list missed it entirely** *[R6→R7]* |

`visible_nav_items` is a **template tag, opt-in per template**, and no template uses the `can`
filter today — so the hazard is latent, not live. That is precisely why it is pinned **by a test
that renders every Stage 5 portal template under the portal role**, written before Stage 5 adds
templates rather than after one 500s.

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

> **RETIRED IN REVISION 7 — the demotion is removed, not re-specified.** *[R6→R7]*
>
> Revision 3 decided *"grant portal write capabilities at `LIMITED`, so `_is_attached_to`
> runs and the object dimension is real."* Both halves are false on the portal surface: the
> object dimension cannot refuse (see the header), and the escalation the demotion was
> written to prevent needs an object the portal cannot fetch.
>
> `documents.transfer` therefore **stays `FULL` for all six roles**. Nothing is edited in
> `deep-research-report.md`, no `RunPython(seed)` migration is needed,
> `PORTAL_FULL_CAPABILITY` keeps its meaning, and the two matrix cells stay green.

**What actually confines a portal write**, stated once so nothing downstream re-derives it
wrongly:

| Layer | Control | Requirement |
| --- | --- | --- |
| Database row | RLS `WITH CHECK` on the child's own `client_id` | **W1** |
| Reference | FK paired on `client_id`, `client_id NOT NULL` | **C3** |
| Reachability | Every portal-readable table is RLS-confined on `app.client_id` | W1b |
| Application | `can()` — **firm-side only; adds nothing on the portal path** | — |

**Consequences of retiring C2** *[R6→R7]*:

1. **`require_can` needs no object resolver, and no startup check.** Both existed to serve
   the demotion. The startup check was also unimplementable as written: `LIMITED` is
   role-dependent, not static — `documents.transfer` is `FULL` firm-side and would have been
   `LIMITED` client-side — so "a portal view gated on a LIMITED capability" is not knowable
   at import time without reading `authz_rolegrant`.
2. **`require_can` still cannot be used on a portal view gated at `LIMITED`.** That defect is
   real and survives independently of C2: it calls `can(request.user, action)` with no object,
   and `_is_attached_to` returns `False` when `obj is None`, so any genuinely-`LIMITED`
   capability becomes an unconditional 403. Two capabilities are already `LIMITED` for
   `client_collaborator` today — `invoices.issue` and `reports.view_financial` — so **a Stage
   5 portal view gated on either 403s for every user right now**. Either gate portal views on
   `FULL` capabilities only, or give `require_can` an object path. This is no longer C2's
   problem; it is a precondition for any portal view that calls `require_can`.
3. **Constraint 5 and success criterion 8 are rewritten below**, not merely narrowed. Their
   `LIMITED` requirement was the demotion's consequence and goes with it.
4. **A pin the table missed** *[R6→R7]*:
   `tests/authz/test_portal_authorization.py::test_the_portal_branch_alone_would_admit_a_stranger`
   carries a comment saying it should fail *if `_is_attached_to` is ever hardened to check
   `user`* — which C2 proposed doing. Retiring C2 leaves it green; had the demotion shipped it
   would have gone red **unpredicted**, which the pins table exists to prevent.

**Superuser — DECIDED rather than left as an either/or** *[R6→R7]*: `resolve_level` and
`granted_levels` both short-circuit `is_superuser` to `FULL` before membership is consulted,
so a superuser holding a client membership passes every portal `can()`. **The portal is gated
on `not is_superuser`**: `_grant_context` refuses a superuser on the portal host outright.
Support access goes through the firm-side app, where it is logged. Leaving this to `can()`
would place the only barrier in the layer this plan has just established adds nothing on the
portal path.

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

> **But `audit_datasubjectrequest` cannot satisfy W1** *[R6→R7 — found by a round-4 reviewer]*.
> Its RLS is **disabled by design**, because a statutory request must be accepted before any
> tenant context exists. W1 requires every write-allow-list table to refuse a forged `client_id`
> **with a named RLS policy in the message** — unsatisfiable where no policy exists. Two ways
> out, and the plan must pick one rather than discover this in Stage 4: give the table a portal
> INSERT policy that coexists with unauthenticated intake, or **keep it off the write
> allow-list** and route portal erasure through a firm-side endpoint. Granting INSERT on an
> unpoliced table is the one option ruled out — it is a portal write with no row-level control
> at all.

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
5. **Portal views are gated on `FULL` capabilities.** `can()` is retained as defence in depth,
   subordinate to RLS, and the plan claims nothing more for it. *[R6→R7: revisions 3-6 required
   `can()` at `LIMITED` "so the object dimension runs". The object dimension cannot refuse
   anywhere on the portal — every portal-readable table is RLS-confined on the same
   `app.client_id` GUC that `_is_attached_to` compares against, so both sides always agree. A
   view gated at `LIMITED` is additionally an unconditional 403, because `require_can` passes
   no object. The enforcing controls are W1 and C3.]*
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
| **W6** | A portal user issued client A's storage key, replaying it as client B, is **refused before any byte is read by the row lookup itself** — `Document.objects.get(storage_key=...)` returns no row under the portal's client-scoped RLS policy, so the view 404s before it ever calls storage. *[R6→R7: revisions 3-6 said "an authorisation check in the Django download view", naming a location and never the check. Both reviewers flagged that the location is not the mechanism. The mechanism is the RLS-confined lookup, which also means this plan's "one layer for the bytes" claim is wrong in the safe direction: the row lookup is RLS-backed, and the view's contribution is refusing to call storage until it succeeds.]* with a positive control (B's own key succeeds in the same test). *[R3→R4: revision 3 delegated the naming to V4, whose three exit criteria pin a backend, key randomness and non-dependence on secrecy, and name no check and no location. Both reviewers flagged the dangling reference. W6's own note already stated the answer, so the delegation was circular]* *[R2→R3: revision 2 said "at the storage layer" — a category error under V4's own recommended outcome, since `FileSystemStorage` has no layer that can refuse anything. The refusal is the Django view, which is this plan's stated single layer, and naming it that way is what makes the requirement satisfiable]* Without this, W1–W5 protect the metadata while the bytes stay open |
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
| `test_the_portal_branch_alone_would_admit_a_stranger` | **no longer breaks** — C2 is retired, so `_is_attached_to` keeps its current `user` handling *[R6→R7: it would have gone red UNPREDICTED had the demotion shipped, since its own comment says so]* | — |
| `COUNTS` in `tests/isolation/test_portal_client_isolation.py` | gains a row when the documents table gets its client policy *[R6→R7: missed by revisions 3-6]* | 3 |
| `PORTAL_FULL_CAPABILITY` | **no longer breaks** — C2 retired, `documents.transfer` stays `FULL` | — |

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
| 3 | **`django-storages[s3]` + `boto3`, and `prod.py`'s `STORAGES` switched to the S3 backend** *[R6→R7: V4 decided object storage and no stage owned the dependency or the settings change; `prod.py` is still `FileSystemStorage`]*. Then **C5**, documents schema, RLS policy, client-paired FKs, `client_id`-leading unique constraints, count-pin bumps — **with the write grant still zero**, so T-056 stays green throughout | V-gates closed |
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
   named check in W6 — **a `Document.objects.get(...)` lookup that returns 404 under RLS**,
   asserted by spying on the storage client to prove it is never invoked.
5. The `DenyPortalAccess` policies refuse a read at runtime, proven without a permanent grant.
6. `app_portal` holds no `DELETE`, `TRUNCATE`, `REFERENCES` or sequence privilege, and write
   access only to the pinned allow-list.
7. Document downloads appear in the Marco Civil access log.
8. Every portal view authorises through `can()` on a **`FULL`** capability, and the controls
   that actually confine the write — RLS `WITH CHECK` (W1) and the client-paired FK (C3) — are
   each proven by a mutation that turns them red.
9. The firm still sees everything it saw before — `pg_has_role('app_runtime','app_portal',
   'USAGE')` false, firm-side row counts unchanged.

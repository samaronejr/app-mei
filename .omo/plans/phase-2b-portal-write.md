# Phase 2b — Portal write access and the document vault — Work Plan

> **Status: DRAFT, not reviewed.** Phase 2a received six adversarial review rounds on the
> plan plus a seventh on the code, and every one found blocking defects — including two in
> the shipped Wave 3 code that its own tests were built to miss. This draft has had none.
> It must go to Momus and Oracle before any todo is implemented.

## Why this phase exists, and what it is really about

Phase 2a built the isolation layer and stopped. It ends at a portal page that renders one
client's name, deliberately unimpressive, and the grant behind it is **SELECT on six tables
and no write privilege anywhere**.

That last fact is doing more work than it appears to. It is the reason a whole class of
Phase 2a machinery has never been exercised:

* Every `WITH CHECK` clause on the eight portal policies is **unexercisable at runtime**.
  The privilege check fires before RLS, so an `INSERT` as `app_portal` is refused before a
  policy is consulted. T-056 asserts those clauses **statically** for exactly this reason.
* The two `DenyPortalAccess` policies on `clients_tag` and `audit_event` are described in
  T-055 as *"the 2b backstop — asserted statically by T-056, never exercised at runtime in
  2a"*. Nothing has ever tried to read those tables as `app_portal` and been refused by a
  policy rather than by a missing grant.

So **Phase 2b's first work is not a feature. It is re-proving isolation under write
grants**, because widening the grant is what turns all of the above from static assertions
into live controls. A document vault built before that is a vault whose isolation has been
asserted but never tested.

## The gate this phase had to wait for

Phase 2a's own text: *"Document upload, notifications and the PWA are Phase 2b, and must
not start until the coverage meta-test in T-056 is green."*

T-056 is green, was re-reviewed after being patched for W1-BL-1/2/3, and every one of its
controls is falsifiable — 18 of 18 T-064 mutation rows produce their expected red. The gate
is open.

## Carried forward from Phase 2a — BOTH must be resolved in this phase

These were found by the Phase 2a reviews, recorded in `.evidence/`, and deliberately not
actioned there because neither could bite while the portal had one read-only view. Both
bite the moment this phase adds a second.

### C1 — `can()` cannot be called inside a portal request

Verified live: a portal request calling `can()` raises
`permission denied for table authz_capability`. `resolve_level` reads `authz_capability`
and `authz_rolegrant`, neither of which `app_portal` holds SELECT on.

Nothing breaks today because `portal_home` uses only `@login_required`. **The first portal
view that needs a capability check will 500.** Since 2b is where portal views multiply and
where writes need authorising, this is a prerequisite, not a nice-to-have.

The fix is a decision, not a detail. Options, to be settled in review:
* grant `app_portal` SELECT on the two authz tables — smallest change, widens the grant
  into tables that are firm metadata rather than client data;
* resolve capabilities **before** `SET LOCAL ROLE` and pass the level down — keeps the
  grant narrow, changes the shape of `can()`'s contract;
* cache the matrix in process at startup — avoids the read entirely, introduces a staleness
  window on a table whose whole design point is that changing a grant is a data edit.

### C2 — `_is_attached_to`'s escalation trigger has fired

`apps/authz/services.py`: the portal branch **ignores its `user` argument** and returns
`str(client_id) == str(portal_client)`. Phase 2a's review established it is safe *only*
because `can()` reaches it exclusively after `role_of()` proved this user holds an active
membership over that client.

Wave 3 was the change that made a client context exist on real requests — the precondition
that branch was waiting on. It is still safe, and two tests pin the coupling. But the
moment 2b adds a view that authorises a **write** against an object, the branch stops being
a redundant re-check of something already proven and starts being load-bearing. **At that
point checking `user` is mandatory, not advisory.**

## Scope

### What this phase covers

1. Widening `app_portal` to the minimum writes the vault needs, and re-proving isolation
   under them — every `WITH CHECK` clause exercised at runtime for the first time.
2. Resolving C1 and C2.
3. A document vault: object storage, upload from the portal, download scoped to one client.
4. Email notification of a new document.

### Explicitly OUT of scope

| Item | Why | Phase |
| --- | --- | --- |
| Web push, service worker, manifest, PWA | iOS reach ≈1–2%; unchanged from 2a | deferred |
| WhatsApp Business API | Costs money, needs numbers we do not collect | 3+ |
| Enabling RLS on the five deliberately-unpoliced tables | Reopens the Phase-1 bootstrap problem | separate |
| Changing any existing RLS **policy** | Restrictive policies compose; 2a's constraint holds | never |
| Portal DELETE of any kind | A client deleting fiscal evidence is a compliance problem, not a feature | never |

## Decided constraints (carried from 2a, still binding)

1. Isolation is enforced by **DB role**, not by a shared GUC. Unchanged.
2. The grant to `app_runtime` stays `WITH INHERIT FALSE, SET TRUE`. `core.E008` enforces it.
3. Writes widen the grant **per table and per verb**. No blanket `GRANT INSERT`. T-056's
   write-privilege assertion becomes an allow-list rather than a flat zero, and that
   allow-list is pinned the way `PORTAL_TABLES` is.
4. `app_portal` never gets `DELETE` or `TRUNCATE` on anything.
5. Every new portal view goes through `can()`. C1 is what makes that possible.

## Verification strategy

The standing commands and the falsification requirement are unchanged from 2a:

> **Any mutation that changes nothing is a defect** — that control is decoration and must be
> fixed or removed.

Two things Phase 2a learned the hard way, which this phase must adopt from the start:

* **Tests that step around the broken path.** Wave 3's fixtures pre-enrolled TOTP, with a
  comment explaining that it avoided testing the enrolment redirect — which was precisely
  the path that 500'd. Assume any fixture convenience is hiding something.
* **Container health is not deployment evidence.** A `--no-build` deploy restarted the app
  on the old image with all six containers healthy, `manage.py check` passing and zero
  migrations applied. Verify by effect.

### New verification this phase specifically requires

| # | Requirement |
| --- | --- |
| W1 | Every portal `WITH CHECK` clause is exercised at RUNTIME: an `INSERT` with a forged `client_id` is refused by the POLICY, not by a missing grant |
| W2 | The `DenyPortalAccess` backstop is exercised at runtime for the first time: a read of `clients_tag` / `audit_event` as `app_portal` is refused by the policy |
| W3 | The write allow-list is pinned, and `roles.sql` remains the oracle — not `conftest` |
| W4 | `app_portal` still holds no `DELETE` and no `TRUNCATE`, anywhere |
| W5 | A portal user cannot write a row attributed to another client, proven at the DB layer with a positive control |

## Todos

*(To be decomposed after review. The ordering constraint is fixed: the grant widening and
C1/C2 come before any vault code, because the vault's isolation claims are only meaningful
once `WITH CHECK` is live and `can()` works inside a portal request.)*

## Success criteria

1. A portal user uploads a document and it is attributed to their client and no other.
2. A forged `client_id` on write is refused **by a policy**, proven at the DB layer.
3. The `DenyPortalAccess` policies refuse a read at runtime, not merely in a catalog
   assertion.
4. `app_portal` holds no `DELETE`, no `TRUNCATE`, and write access only to the tables on the
   pinned allow-list.
5. Every portal view authorises through `can()`.
6. The firm still sees everything it saw before — `pg_has_role('app_runtime','app_portal',
   'USAGE')` false, firm-side row counts unchanged.

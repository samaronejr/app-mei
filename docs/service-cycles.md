# Service-cycle integration contract

Cycle writes live in `apps/cycles/services/cycle.py`. Call them inside a tenant
context. Firm callers are resolved through the portfolio, then the aggregate is
locked, its rendered revision is compared, authorization is checked again, and
the event is inserted in the same transaction. Conflicts write nothing.

Generation is an INSERT with `ON CONFLICT DO NOTHING RETURNING id`, not a pre-count.
The natural key is `(tenant, client, engagement, playbook, period_start)`; the
caller key is hashed for correlation only. The current fiscal-profile row and
published playbook version are pinned at creation. Replaying with a newer
published version returns the existing cycle without replacing its tasks.

The scheduler runs on the first day at 05:00 America/Sao_Paulo. It visits enabled
firms through `TenantTask`, and stamps `generate_cycles` only after the sweep
succeeds. It performs no external I/O. Retrying an interrupted transaction is
safe: graph creation and its event roll back together.

## Extension points

- Capability constants use the live S8.1 `cycles.operate`, `cycles.review`,
  `cycles.complete` and `cycles.reopen` matrix rows. Client loading uses the
  shared portfolio loader through the maintenance helper, including its locking
  variant; there are no legacy capability stand-ins.
- `register_exception_reason` registers deterministic reason policies at startup;
  `open_exception` converges on one active `(cycle, reason, subject)` problem.
  Its owner must be an active firm member. It requires a due or review date.
- `COMPLETION_GUARDS` holds domain checks. The evidence-index producer must
  register `evidence_index`; without it completion fails closed. Requests,
  proposals, effects, exports and reopening execution are separate downstream
  domains, not simulated here. `CycleAmendment` stores the prior snapshot and
  approval reference for the later reopening service.
- `EFFECT_GUARDS` lets an effect-producing domain report executed effects under
  the cycle lock. Cancellation refuses when any registered guard reports one.
- `OUTBOX_TIMELINE_READERS` accepts readers with explicit tenant, client and period
  arguments. Return minimized `TimelineEntry` references, never message bodies.
  The empty registry means delivery is not integrated, not that nothing was sent.
- Coverage is explicitly unknown until observation readers are integrated. Manual
  fiscal profiles and legacy obligation states never render as official proof.

## Portal and persistence

Only `cycles_servicecycle` is SELECT-granted to the portal. Every other new table
has an explicit NONE decision in `roles.sql` and `tests/conftest.py`. All six
client tables use tenant RLS and RESTRICTIVE client RLS, matching the existing
client-contact NONE convention. There are no views and no portal write grants.
The portal projection contains only period, work-state label and next action.

Cycle events contain ids, codes, hashes and timestamps. Reasons and notes live in
private tables, not event payloads. The append-only migration reuses the audit
reject function, but its reversal drops only its own trigger: other audit tables
must retain their protection on a populated database.

The flagged workspace replaces the legacy client detail without changing the
flag-off route. Not-yet-integrated request, coverage and communication sections
state their limitations rather than presenting empty data as verified absence.

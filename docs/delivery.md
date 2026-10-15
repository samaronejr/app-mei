# Durable SMTP delivery

`apps.delivery.outbox.enqueue` accepts a portfolio-authorized client, one active
email-enabled ClientContact id, a BusinessIntent and the client's rendered version.
It never accepts an address, uploaded filename or arbitrary template. The natural
intent hashes the tenant, engagement, playbook, period, amendment, step, action and
occurrence; message/proposal UUIDs are not identity. Digest mismatches are conflicts.

`policy.register_kind` registers a shipped template and a `still_valid(message)`
hook. Hooks run in the final DB phase before egress and must not perform external
I/O. Portal invitations and request/reminder messages register their live pre-send
guards in `apps.delivery.kinds`.

## Dispatch and limits

The minute dispatcher visits only active `cycles_r1` tenants. It commits SKIP LOCKED
claims, performs journal/SMTP I/O outside transactions, and finalizes behind a fence.
A gateway fence authorizes one send. Expired sending leases become uncertain, not
reclaimed. A sender that finishes after uncertainty is recorded cannot regress that
state; its independent receipt can be inspected through reconciliation.

SMTP DATA acceptance is not inbox delivery or a read receipt. Ambiguous disconnects
and socket errors do not retry. Explicit temporary replies retry after 1, 5, 15, 60,
and 60 minutes (six attempts total). `/comunicacao/` records staff decisions separately
from SMTP evidence. Neither manual decision initiates another transmission.

`DELIVERY_PAUSED=True` is the default everywhere. It overrides the tenant pause in
Comunicação. Changing the environment requires coordinated restart of web, workers
and beat; pause is not a way to revoke an SMTP exchange already in progress.
`DELIVERY_DAILY_CAP` is a per-tenant cap on attempt reservations, not accepted emails.
Failed or cancelled-after-claim attempts consume a reservation. The local calendar
day resets the quota; previews can call `services.remaining_quota`.

## Automation management and readiness

`/gestao/automacoes/` requires `automations.manage` and `cycles_r1`. It shows
the registered beat jobs, their recorded completion heartbeats and estimated next
runs, using persisted scheduler overrides when present. A dispatch timestamp is
not a completion. Missing heartbeats remain visibly unknown. These are platform
signals, not proof that one client's work ran. The message list and its counts
are confined to the actor's portfolio.

Pause/resume and known-refusal retry/cancel use row locks, the rendered revision,
fresh authority and audit events. A retry never resets or deletes an immutable
`DeliveryEffect`: it creates one same-client successor with a deterministic key
derived from the parent's intent. A restored journal can therefore prevent replay
even when the successor's original INSERT is missing. Approved mail retains its
frozen content and root approval identity; each retry link is checked at egress.
Reverse/reapply of the retry migration recovers links from the immutable retry
audit references without rewriting message, attempt or effect history. Keep
dispatch paused during schema rollback and reapplication.
Uncertainty and staff-reported outcomes are never retry authorization. Changed or
revoked invitations need their normal reissue workflow rather than bypassing the
pre-send guard.

`/readyz` returns 503 with `reason=outbox_oldest_queued_age` when the oldest queued
or temporarily failed message exceeds `OUTBOX_MAX_QUEUED_AGE_SECONDS` (900 by
default), or `reason=outbox_dead_letters` when rejected/uncertain messages exceed
`OUTBOX_MAX_DEAD_LETTERS` (0 by default). Boundaries are strictly greater-than;
zero is a strict limit, not a disabled check. Pauses do not hide backlog.
The public response exposes no tenant ids, recipients, message ids or counts.
Its single fixed aggregate query does not open a tenant transaction or grant
row access to the portal. `/healthz` and its existing monitor keywords are unchanged.

## Restore procedure

Stop dispatchers before restoring. Restore the DB but **do not roll back object
storage's `effects-journal/` prefix**. Start with global delivery paused, and run:

```sh
DELIVERY_PAUSED=true uv run python manage.py reconcile_effects
```

A successful command leaves the switch enabled. Accepted intents are not resent;
started-without-reply intents become uncertain. Natural-key barriers also protect
messages whose outbox insert was lost. Missing tenant scopes, conflicting digests,
malformed records and storage failures abort reconciliation. Reconcile inactive
firms too, because they may later be reactivated. Resolve missing scope data before
retrying; do not discard journal records to obtain a successful result.

Inspect uncertain/rejected messages and exceptions, then explicitly enable dispatch
only after all operator gates are met. Re-running reconciliation is safe. Never
infer success from a zero count alone: inventory/read errors must have a nonzero exit.
Journal records contain only identifiers, hashes, codes and timestamps. Start and
reply use different UUID-suffixed immutable objects through Django storage.

The complete ordered procedure is `ops/RESTORE.md`, **Axorim restore**:
global pause first, database and object recovery, document reconciliation,
effect reconciliation, uncertainty review, then deliberate unpause. The local
engineering rehearsal does not close the isolated-host operator drill OG-06.

## Open operator gates

- **OG-03:** real SMTP send from the production host to an owner-controlled mailbox;
  establish Resend quotas and set the attempt cap accordingly. Local aiosmtpd tests
  prove protocol behavior, not provider deliverability.
- **OG-04:** configure and verify independent retention/versioning of the private
  `effects-journal/` prefix in OCI storage, outside DB restore scope. Filesystem tests
  do not prove bucket retention, IAM or a live restoration drill.
- **OG-06:** the operator's isolated-host restore-replay drill, with backup
  provenance, timings and review recorded. OPEN after repository/local QA.

No gate is closed by this implementation. Keep sending paused until all are recorded.

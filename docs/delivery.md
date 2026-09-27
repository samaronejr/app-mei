# Durable SMTP delivery

`apps.delivery.outbox.enqueue` accepts a portfolio-authorized client, one active
email-enabled ClientContact id, a BusinessIntent and the client's rendered version.
It never accepts an address, uploaded filename or arbitrary template. The natural
intent hashes the tenant, engagement, playbook, period, amendment, step, action and
occurrence; message/proposal UUIDs are not identity. Digest mismatches are conflicts.

`policy.register_kind` registers a shipped template and a `still_valid(message)`
hook. Hooks run in the final DB phase before egress and must not perform external
I/O. Request/reminder integrations belong to their owning domain; contact invitations
remain on their existing transaction.on_commit path until that integration ships.

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

## Open operator gates

- **OG-03:** real SMTP send from the production host to an owner-controlled mailbox;
  establish Resend quotas and set the attempt cap accordingly. Local aiosmtpd tests
  prove protocol behavior, not provider deliverability.
- **OG-04:** configure and verify independent retention/versioning of the private
  `effects-journal/` prefix in OCI storage, outside DB restore scope. Filesystem tests
  do not prove bucket retention, IAM or a live restoration drill.

No gate is closed by this implementation. Keep sending paused until both are recorded.

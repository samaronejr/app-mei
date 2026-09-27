# 0004: PostgreSQL holds all workflow state; client-facing effects go through an outbox

- **Status**: Accepted
- **Date**: 2026-09-27
- **Gate**: AXR1-001 of the local-only plan `axorim-pivot-r1` (see `docs/project-status.md`)
- **Applies to**: every state change and every external effect added by the R1 release

## Question

R1 introduces client-facing effects that must happen exactly once and only as approved:
publishing a document to the portal, sending an email, completing a cycle, waiving a
request. How is state kept, and how does an effect leave the system without being lost,
duplicated or sent on stale data?

Three facts from the current code shape the answer:

- Email is sent synchronously with `send_mail` (`apps/accounts/views.py:185`). There's
  no outbox, no delivery state and no idempotency key.
- `tenant_context` opens an outer `transaction.atomic()` (`apps/core/tenancy.py:93`),
  and `PlatformTask.each_tenant()` yields inside it (`apps/core/tasks.py:63-75`). Any
  network call made from inside that loop runs inside a database transaction.
- Celery tasks already use late acknowledgement and requeue on worker loss, so a task
  can run twice.

## Decision

### State

PostgreSQL is the only store of workflow state. No broker, no workflow engine, no
second database.

- Every firm-side state change goes through a domain service that takes a row lock,
  checks the row version, re-checks the actor's capability, and writes a domain event,
  all in one transaction.
- Portal-side actions only insert rows (links, intakes, events, acknowledgements).
  States the client drives are derived from those rows; the portal role never updates
  them.
- Domain events go to an append-only cycle event table with a fixed envelope: event
  id, type, schema version, tenant, client, actor, aggregate type, id and version,
  occurred and observed timestamps in UTC, correlation and causation ids, and a
  minimized payload of ids and codes. Document text and message bodies never go into
  an append-only table, and R1 never purges those tables.

### External I/O never runs inside a transaction

Every external call (SMTP, malware scan, model call, export upload) follows
claim, then I/O, then finalize:

1. **Claim.** Inside the tenant context, select due rows with
   `FOR UPDATE SKIP LOCKED`, mark them in flight, and set a lease owner, a lease expiry
   and a monotonically increasing fence. The context exits and commits.
2. **Send.** Outside any transaction (the code asserts `not connection.in_atomic_block`),
   write a journal record and make the call.
3. **Finalize.** Back inside the tenant context, update the row only if the fence is
   still current, and record the attempt and the effect.

The dispatcher collects tenant ids first and opens a separate tenant context per phase,
so the per-tenant loop never holds a transaction across the network.

### Outbox and delivery states

Client-facing messages are rows in an outbound message table with an idempotency key
unique per tenant and client. States only record what SMTP can actually evidence:

| SMTP result | State | Retry |
| --- | --- | --- |
| 2xx | accepted by the provider (not "delivered") | none |
| 4xx | temporary failure | backoff of 1, 5, 15 and 60 minutes, at most 6 attempts |
| 5xx | rejected by the provider | none |
| connection lost after DATA | outcome unknown | **never automatic**; an exception is opened for a person |

A transition table rejects regressions. Late provider events are recorded as attempts
without moving the state. A global delivery pause and a per-tenant automation pause are
honoured by the dispatcher.

Reminders are re-evaluated just before sending: the request must still be open, the
recipient still an active contact with permission, the cycle not completed and the
content digest unchanged. Otherwise the message is cancelled with a reason.

### Idempotency, fencing and the effect journal

- **Business intent key**: SHA-256 over the canonical form of tenant, engagement,
  playbook, period start and end, amendment, step, action kind and occurrence. The
  effect table is unique on tenant, client and that key. The same key with a different
  content digest is a conflict, never a cached success. The SMTP `Message-ID` (derived
  from the outbox id) is for correlation only.
- **Fencing**: the egress gateway refuses a send whose fence isn't current. An expired
  in-flight lease becomes "outcome unknown"; it's never re-run.
- **Effect journal outside the database**: before each send the gateway writes an
  `intent_started` record, and after the reply an accepted, rejected or unknown record,
  as append-only objects in a separate versioned bucket prefix. After a database
  restore, a reconciliation command reads the journal first: accepted intents missing
  from the database are marked accepted and not re-sent, started intents without a
  reply become "outcome unknown", and sending stays disabled until reconciliation
  finishes. A ledger restored together with the database can't prevent a replay,
  which is why the journal lives outside it.

### Exact-effect approval (appendix)

Consequential effects (client publication, free-text email, cycle completion, request
waiver) need approval of the exact effect, by someone other than the initiator by
default. Templated notices (request notice, reminder, portal invitation) run under the
published playbook version or tenant policy instead, still through the outbox and the
pre-send re-evaluation.

- The effect is described by a closed, versioned manifest, schema id
  `axorim.effect.v1`, covering tenant, client, engagement, cycle and revision, period,
  playbook version and hash, proposal and revision, business intent key, action kind,
  destination, fully rendered payload, recipients, money, documents with their hashes,
  evidence, observations, tax-profile versions, policy, the initiator's authorization
  snapshot, the approval rule, and the time bounds.
- The persisted bytes are RFC 8785 (JCS) canonical JSON, UTF-8 without BOM, and the
  digest is lowercase hex SHA-256 of those bytes. Floats, duplicate keys and unsafe
  integers are rejected; decimals are fixed-scale strings; UUIDs and hex are lowercase;
  set-valued arrays are sorted by canonical member bytes.
- Approvals are append-only rows bound to the proposal revision and digest. The approval
  screen renders the frozen manifest, not live data.
- At execution, the manifest is rebuilt from live data under row locks and the digest
  recomputed, and memberships, grants, quorum, cancellation and time bounds are
  re-checked. Any mismatch invalidates the approval with reasons and opens a new
  proposal revision. Approvals expire after 72 hours by default.
- Golden test vectors for the canonical digest are part of the todo that builds the
  approval service.

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| Keep synchronous `send_mail` for client-facing mail | No record of what was sent under which approval, no safe retry, and a network call inside the request transaction. |
| A message broker (Kafka, RabbitMQ streams) as the event backbone | New infrastructure and a second state store outside RLS, for volumes a single PostgreSQL handles. |
| A workflow engine holding case state | Same objection as in ADR 0003: state outside RLS, plus paid or self-hosted infrastructure. |
| Automatic retry on every failure | Unsafe after an ambiguous SMTP result: a second send can't be taken back. Only clear temporary failures retry. |

## Consequences

- Every client-facing effect has an auditable path from proposal to approval to attempt
  to result, and "accepted by the provider" is never shown as "delivered".
- An ambiguous send costs a person's attention, never a duplicate email.
- Restores need the reconciliation step before sending resumes; the recovery runbook
  gains that step when the outbox ships.
- Staff invitations and the data-subject-request notifier stay synchronous. They're
  firm-internal or anonymous-requester flows, and they're allowlisted by name.

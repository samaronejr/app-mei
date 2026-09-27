# 0003: Bounded contexts for R1, with new `cycles` and `delivery` apps

- **Status**: Accepted
- **Date**: 2026-09-27
- **Gate**: AXR1-001 of the local-only plan `axorim-pivot-r1` (see `docs/project-status.md`)
- **Applies to**: every table, service and Django app added by the R1 release

## Question

R1 makes the client-period service cycle the unit of work: for one client and one
period, the firm tracks what it asked for, what arrived, who reviewed it, what went out
under whose approval, and what's still unknown. That needs roughly thirty new tables.
Where do they live, and which boundaries do they respect?

Today there is no case, task, playbook, outbox or delivery domain anywhere under
`apps/`. Work and payment share a single `Obligation.status`
(`apps/obligations/models/operations.py:57-64`), and email goes out through synchronous
`send_mail` calls (`apps/accounts/views.py:185`).

## Decision

Group the work into the contexts below. Existing apps keep their tables and names; the
two new domains get their own apps so their invariants live in one migration graph each.

| Context | Django app | Adds in R1 | Notes |
| --- | --- | --- | --- |
| Isolation kernel | `core`, `tenants`, `accounts`, `authz`, `security`, `audit`, `lgpd` (existing) | `tenants_tenant.features` (array), `authz_usercapabilitygrant`, `tenants_supportgrant` | Guarantees unchanged |
| Client registry | `clients` (existing) | `clients_engagement`, `clients_taxprofile`, `clients_clientcontact`, `clients_importbatch`, `clients_importrow` | `clients_clientcompany` is not renamed |
| Fiscal reference | `fiscal`, plus the reference tables in `obligations` (existing) | a tax-regime choice list (enum, no table) | Platform data |
| Obligations calendar | `obligations` (existing) | `obligations_sourceobservation`, one table for every observation | The document table stays here |
| Service cycle | `cycles` (new) | playbooks and their versions and steps, the service cycle, tasks, evidence requests and their internal notes, evidence links, document intakes, reviews, extracted fields, exceptions, internal notes, action proposals, approvals, client publications, client acknowledgements, the append-only cycle event log, evidence exports, bulk operations and items, saved views | One migration graph for every case invariant |
| Delivery | `delivery` (new) | outbound messages, send attempts, the append-only effect log, and an inbound event table reserved for R2 webhooks | Portal invitations move onto it in R1. Firm-staff invites and the data-subject-request notifier stay synchronous and explicitly allowlisted |
| Briefing | `briefing` (new) | no tables; a read model over `obligations` and `cycles` | Saved views live in `cycles` |
| Automation | `automation` (new, minimal) | agent runs and evaluation runs | Shadow mode only in R1; nothing it produces reaches a user or an effect |
| Portal | `portal` (existing) | no tables; presentation only | |

Rules every new table follows, in the same PR that creates it:

- Row-level security, with client-scoped tables using the composite tenant and client
  foreign keys. The existing coverage meta-tests
  (`tests/isolation/test_rls_coverage.py`, `tests/isolation/test_portal_rls_coverage.py`)
  fail on any table that skips this.
- Every unique constraint on a client-scoped table includes `client_id`
  (`tests/isolation/test_client_scoped_uniqueness.py`).
- An explicit portal decision: grants in `ops/sql/roles.sql` and the matching entry in
  the portal and append-only table lists in `tests/conftest.py`. No portal database
  views unless they use `security_invoker=true`.
- Firm-side state changes go through a domain service (see ADR 0004). Portal-side
  actions only insert rows; any client-driven state is derived, never updated by the
  portal role.

Concepts that get **no table in R1**: department or team (tags and assignments cover
it), delegated authority, establishment or branch, service contracts and time entries,
connector connections and cursors (the observation table reserves their source kinds),
and versioned agent policies. Portfolio risk, briefing items, the coverage matrix and
the monthly value report are derived views, not tables.

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| Put cycles, requests and approvals inside `obligations` | `obligations` is the deterministic calendar. Mixing case workflow into it couples two migration graphs and blurs the split between work status and payment status that R1 exists to fix. |
| Microservices, or a separate delivery service | New infrastructure, a network boundary inside every approval, and a second place where tenant isolation must hold. The team size and the pilot host don't justify it. |
| An external workflow engine | The workflows are short, per-client and need row-level isolation inside PostgreSQL. An engine would hold state outside RLS and add paid or self-hosted infrastructure. |
| Rename `ClientCompany`, or replace it with a new client model | Churns every foreign key and test for no behavioural gain. Engagements and tax profiles hang off the existing table instead. |

## Consequences

- Each new app starts with the isolation pattern, not as a retrofit. A table that
  misses it turns the isolation suite red before merge.
- The `delivery` app's egress gateway becomes the only client-facing sender. The two
  remaining synchronous `send_mail` call sites are staff invitations and the
  data-subject-request notifier, and they stay on an explicit allowlist.
- The context map above is the default home for later work. A new table that doesn't
  fit it needs an amendment to this ADR, not a quiet addition.

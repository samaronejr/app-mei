# Provider budgets (AXRM-010)

Provider adapters register their domain authority predicate through
`BudgetPolicy(authority)`. The authority predicate still owns authority and
approval checks; the policy owns pricing, reservations and settlement. No
operation or real-provider credential is registered by this delivery.

## Monthly configuration

Provision a `ProviderBudget` for each tenant/provider/UTC calendar month before
enabling that provider. The absence of the row disables admission. The budget is
shared across environments and clients; a new month does not inherit a previous
month's cap. Amounts are integer micro-BRL (one BRL is 1,000,000 micro-BRL).

Set `cap_microbrl`, a nonempty `tariff_revision`, and the corresponding decimal
entries in `USAGE_UNIT_PRICES_BRL`. Missing, negative, non-finite or
sub-micro-BRL prices refuse admission. Set the SERPRO platform ceiling in
`PROVIDER_GLOBAL_MONTHLY_CAP_BRL`; it includes inactive firms' existing exposure.
The transaction-wide advisory lock and each tenant's RLS context serialize the
platform aggregate without a cross-tenant query or a privileged budget function.

For Focus, set `included_documents` to the explicitly authorized plan allowance
and provision the overage cap (provisional 50,000,000 micro-BRL until the owner
sets it). Included document/configuration/Ciencia requests use an explicit zero
tariff, initially `focus-plan (PQ-FOCUS-4 pendente)`. Only revisions listed in
`ZERO_TARIFF_REVISIONS` reserve zero; any other Focus revision is an absent price
and refuses (`tarifa_ausente`). Record a new tariff revision in code when
qualification changes that assumption; old reservations keep their original
revision. PQ-FOCUS-4 and owner authorization remain open. A missing SERPRO
platform ceiling refuses paid calls with `cota_ausente`.

## Attempt accounting

The call is persisted inside the reservation transaction before the
`ProviderReservation` referencing it is inserted. A refused reservation commits
the blocked call and owned quota issue, then raises `BudgetDeniedError`; it never
returns permission for I/O. A duplicate intent does not reserve again.

One reservation is keyed by tenant, client, call and attempt number. Retries
therefore have separate balances. Claim and dispatch recheck the held reservation
and current caps. An untransmitted refused dispatch releases its reservation.
An uncertain transmitted attempt retains maximum exposure until reconciliation.
The journal's original month, amount and tariff rebuild a lost reservation;
the corresponding budget must exist before reconciliation can complete.

SERPRO Consulta and Emissao responses 200, 202 and 403 consume their reserved
unit even when the domain result is an error. Other documented statuses release
it. Explicit unbilled endpoints never consume a paid unit. Unknown status retains
uncertain exposure. The production policy, rather than an adapter's billing hint,
classifies these outcomes before journaling and finalization.

Confirmed daily usage references the call ID on its first attempt. Additional
transmissions use a deterministic call/attempt UUID because AXRM-009 keeps read
retries on the same call. Reconciliation appends a correction of an estimated
measurement instead of rewriting immutable usage.

## Focus document units

The fiscal register producer passes its immutable capture identity to
`record_captured_revision(link_id, CapturedRevision(...))` in the revision-ingest
transaction. The input is the register's tenant/client/document/revision identity
and observed timestamp, never values taken as tenant authority from a provider
payload. This foundation does not create the fiscal register owned by AXRM-020.

Reads, page polls and repeated XML downloads do not produce document usage.
Captured revisions produce one estimated unit per document/UTC month, including
replays of a revision. Estimated overage begins beyond `included_documents`.
Ciencia attempts produce estimated `focus_eventos`, separately from documents.
All Focus calls retain the independent journal; effects retain their barriers.

At 80% and 100% the budget opens firm-private owned work. Existing current-month
cycles receive the corresponding exception. The account panel distinguishes
measured, estimated and missing usage.

Focus is labelled "limite do Axorim - nao e um teto do provedor". Captured
evidence above the local ceiling is still recorded: provider-side capture is not
bounded by our HTTP requests. At exhaustion the operator must use the
`focus_company_disable` workflow and verify the provider read-back; pausing local
workers alone cannot stop upstream collection. That command belongs to AXRM-019.
Explicitly zero-cost Focus configuration remains admissible above the overage
threshold so the operator can perform that shutdown; paid exposure is never exempt.

## Recovery

Pause providers before recovery. Reconcile independent receipts before resuming
calls; do not refund unknown outcomes or retry ambiguous effects. Usage remains
append-only, and missing restoration configuration fails closed.

Migration `integrations.0003` adds only the two private budget tables and their
policies/foreign keys. Reversal is allowed only while both are empty, and removes
only those objects. Populated balances require forward repair with tables kept.
No shared append-only function, existing usage metric, or provider-call table is
removed by its reverse.

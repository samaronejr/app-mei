# Retention regimes

Retention depends on the stream, its fiscal period and any active legal hold.
The R1 periods below are provisional pending professional review at OG-08.
`RETENTION_PURGE_DRY_RUN` defaults to `True`: the scheduled job reports counts
without deleting bytes, redacting fields or creating purge claims. The owner must
resolve OG-08 before enabling deletion. OG-04 still gates the production bucket
prefixes, versioning and lifecycle configuration.

| Stream | Table | Retention | Why | Enforced by |
| --- | --- | --- | --- | --- |
| Access log | `audit_accesslog` | **6 months, then purged** | Marco Civil da Internet (Lei 12.965/2014) art. 15: application providers must retain access records for six months. It is a duty to keep *and* a ceiling — holding longer converts a compliance obligation into a standing liability. | `apps.audit.tasks.purge_access_logs`, on the Celery beat schedule |
| Business audit | `audit_event`, `audit_platformevent` | **Not purged in this phase** | These record who changed what in a firm's own data — role changes, exports, DAS confirmations, impersonation. They are the evidence a firm relies on in a dispute, and they are append-only at the database. | Append-only trigger + enumerated grants; no purge task exists |
| Fiscal evidence | `obligations_document`, document-derived fields and review text | **At least 5 years after fiscal year-end** | R1 uses January 1 of fiscal year + 6 in `America/Sao_Paulo`. Shared originals use the latest linked cycle or obligation year; an unknown year retains the content. The statutory period per document type remains OG-08. | `apps.cycles.retention_tasks.purge_expired_content`; original bytes and derived text are removed together |
| Operational content | Internal notes, request/exception instructions, amendment reasons, completion justifications and non-communication proposal text | **5 calendar years** | Keeps the working explanation for the provisional operational period. Immutable generations preserve only identifiers, revisions, decisions and hashes, not this prose. | Same purge job; content-only redaction |
| Communication bodies | `delivery_outboundmessage`, communication proposals, `cycles_clientpublication` | **2 calendar years** | Redacts subject/body and rendered MIME copies, preserving the envelope, original digest, execution and acknowledgement identities. It does not rebind old acknowledgements to later publications. | Same purge job and exact redaction guards for frozen records |
| AI-derived content | `cycles_extractedfield` explicitly classified `ai_derived` | **1 calendar year or removal with its source** | No live model integration is enabled by this classification. Existing deterministic extraction remains `fiscal_evidence`. | Same purge job |
| Evidence ZIPs | `cycles_evidenceexport`, storage prefix `exports/` | **30 days after generation** | Disposable copies of the evidence, not additional originals. Download expires at the timestamp even while a hold delays byte deletion. | Same eligibility/hold decision and purge job; originals are not deleted by export expiry |

`RetentionClass` is stored on originals, evidence links, messages and extracted
fields. Unrecognized original classifications fail closed. Evidence links and
immutable history retain their small identity/hash envelopes; R1 never bulk-deletes
those metadata rows. Whether any additional metadata must be erased after its
operational period is part of OG-08, not an implied legal approval.

## Holds and deletion claims

`LegalHold` is private and client-wide, covering all engagements and shared
originals. Placement and release require scoped staff authority and the version
shown to the caller. Both create audit events. An active hold blocks every R1
content purge and export-expiry deletion.

Deletion takes a client lock, recomputes eligibility, and commits a scoped
`ContentPurgeClaim` containing only the exact owned storage keys. Storage I/O runs
outside any database transaction. A later transaction redacts content and records
completion; storage failures leave the claim recoverable on the next sweep.
Hold placement refuses while an earlier deletion claim remains unfinished:
the claim is the explicit point after which deletion may already have occurred.
It must be resolved before a new hold can promise protection.

New document links serialize with that same client lock and refuse originals
whose deletion has been claimed. Pending/building exports extend source retention
through their 30-day request window, and ready exports through their actual expiry.
A claim prevents a new export from reading the original. Raw and cascading
metadata deletes cannot bypass retained-document, link, message, provenance,
private explanation, legal-hold or export-recovery protections. Content redaction
keeps those identities rather than deleting their rows.
The existing access-log purge, scanner/intake compensation and import rollback
rules remain separate; an existing legal hold is an import-rollback dependent.

## Evidence archive limits and recovery

Archives use `ZIP_STORED` and a 16 MiB spooled temporary file. The 500 MiB cap is
checked against actual bytes read and the closed archive extent, including ZIP
overhead. Metadata has a separate 8 MiB limit to bound static HTML rendering in
the 320 MiB worker. Oversized metadata is refused explicitly rather than truncated.
`manifest.sha256` lists each member except itself; the stored archive hash is
computed after closing the ZIP.

S3 originals are read directly from the streaming `GetObject` body, so the byte
counter bounds network reads rather than an already-downloaded backend buffer.
Downloads are firm-only and app-mediated. Their iterator receives a fixed
descriptor, performs no ORM work and closes storage handles on disconnect.

Each attempt owns a unique storage key and fence. Workers have an explicit
600-second hard limit and a 15-minute lease. Finalization refuses expired or
superseded leases, changed sources and revoked authority. Recovery fences expired
building work before retrying it and cleans only abandoned attempt keys, never
the ready winner. Failed cleanup keeps its durable marker for the next sweep.

## Why the access log is a separate table

It would have been simpler to add a row type to `audit_event`. Three things make that
wrong:

1. **Different retention.** Purging `audit_event` to satisfy a six-month rule would
   destroy business audit records that must survive. Storing access records for years
   would hold personal data with no lawful basis.
2. **Different immutability.** The business audit trail is append-only and rejects
   `DELETE` at the database, for every role including the table owner. The access log
   must be deletable — a purge that cannot delete is not a purge.
3. **Different tenancy.** Access records exist for anonymous and pre-authentication
   requests, which have no tenant. `audit_event` is row-level-security scoped and
   would reject those inserts outright.

## Why the access log has a tenant column but no policy

Without a tenant column a firm cannot be answered when it asks "who accessed my
clients' data?" — a question the Marco Civil framework contemplates and a firm will
eventually ask. With a fail-closed row-level-security policy, every anonymous and
pre-authentication request would be **refused at insert**, losing exactly the records
an intrusion investigation starts from.

So the column is nullable, the table is listed in `apps.core.rls.NON_TENANT_TABLES`,
and scoping is applied in the application layer through `AccessLog.objects.for_user()`
— which is a named, tested control rather than a convention.

## Deletion requests and this table

A data-subject erasure request does **not** automatically empty either stream. Access
records are held under a legal obligation, which is an independent lawful basis;
business audit records are held for the firm's own defence and are append-only by
design. `docs/lgpd.md` carries the response template that says so.

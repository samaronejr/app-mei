# Retention regimes

Three different obligations govern how long this product keeps three different kinds
of record. They are documented side by side because the usual failure is applying one
rule to all three — which either destroys evidence a firm is required to hold, or
keeps personal data long past any lawful basis for holding it.

| Stream | Table | Retention | Why | Enforced by |
| --- | --- | --- | --- | --- |
| Access log | `audit_accesslog` | **6 months, then purged** | Marco Civil da Internet (Lei 12.965/2014) art. 15: application providers must retain access records for six months. It is a duty to keep *and* a ceiling — holding longer converts a compliance obligation into a standing liability. | `apps.audit.tasks.purge_access_logs`, on the Celery beat schedule |
| Business audit | `audit_event`, `audit_platformevent` | **Not purged in this phase** | These record who changed what in a firm's own data — role changes, exports, DAS confirmations, impersonation. They are the evidence a firm relies on in a dispute, and they are append-only at the database. | Append-only trigger + enumerated grants; no purge task exists |
| Fiscal artifacts | *(Phase 3+)* | **≥ 5 years** | Tax and social-security documents carry statutory retention measured in years, set per document type. Nothing in Phases 0–1 stores them yet. | Not yet implemented |

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

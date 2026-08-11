# Pilot metrics and stop conditions

The pilot runs with one cooperative firm, 10-30 MEI clients, and one real monthly
cycle. This document says what we measure, where each number comes from, how often
someone looks at it, and who that someone is. It also says what makes us stop.

**Read this before the pilot starts.** The metric list is closed. Every row below is
sourced from a surface that already exists in the deployment, and adding a metric
means adding collection, which means adding personal data. That is the trade we
declined.

## The rule that shapes the list

No new tracking code. No analytics script. No new personal-data collection of any
kind. LGPD data minimisation (see `docs/lgpd.md`) is not a checkbox we passed once;
it is why this list is short and why some of the numbers below are coarser than a
product team would like. A coarse number from a log we already keep beats a precise
number from a beacon we would have to install.

Everything here is derived from six surfaces:

| Surface | What it is | Status |
| --- | --- | --- |
| Sentry | Error and exception reporting, DSN-gated, scrubbers pinned by tests. Receives invitation send failures, LGPD notification failures, storage inconsistencies, and rate-limit-store outages. | Configured in code. Live once the DSN is set on the target host. |
| UptimeRobot | External HTTP checks against `/healthz`, `/readyz`, and the public pages. Supplies uptime and response time. | Endpoints live in the codebase. **Account and monitors not yet created — gated on PILOT-207.** |
| Healthchecks.io | Dead-man checks pinged by the scheduled jobs (backup, off-host copy). Silence is the signal. | Ping sites live in `ops/backup.sh`. **Checks not yet created — gated on PILOT-208.** |
| gunicorn access log | stdout access log. Format is fixed in `docker-compose.prod.yml`: remote, time, method, status, bytes, duration, user agent. **It carries no URL and no path.** | Live. |
| Django admin | Registered model admins. `apps/audit/admin.py` registers `Event`, `PlatformEvent`, `AccessLog`, and `DataSubjectRequest`; `apps/tenants/admin.py` and `apps/clients/admin.py` register the tenancy and client models. Counts come from list views and filters. | Live. |
| `AccessLog` table | `audit_accesslog`, written by `AccessLogMiddleware` for every non-exempt request: tenant, user, IP, user agent, method, path, status, timestamp. Retention is **180 days**, then purged (`ACCESS_LOG_RETENTION_DAYS`, `apps.audit.tasks.purge_access_logs`). | Live. |
| Support notes | What the operator writes down after a call, a message, or a session. Plain text, kept with the pilot evidence. | Live. |

Two notes that matter and are easy to misread:

**The gunicorn access log has no URL, on purpose.** The format string omits the request
line, so a URL can never carry a token, a reset key, or a client identifier into a log
aggregate. The cost is real: per-endpoint 4xx/5xx and per-endpoint latency are
**deliberately unavailable** from that surface. That is a credential-hygiene decision,
not an oversight. Do not add `%(U)s` to get a nicer dashboard.

**The `AccessLog` table does have a path**, because Marco Civil art. 15 requires an
access record. It is credential-redacted at write time for registered
credential-bearing routes, tenant-scoped on read through
`AccessLog.objects.for_user()`, and purged at 180 days. It is a legal record we query
by hand when we need a per-path number, not a metrics pipeline. Treat every query
against it as touching personal data.

## Part 1 — Metrics

Owner is `operator` unless a row says otherwise. Roles are named in
`ops/PILOT-RUNBOOK.md`; the same person may hold several.

| # | Metric | Source | Cadence | Owner |
| --- | --- | --- | --- | --- |
| 1 | Invitation delivery | Sentry (invitation send failures are reported explicitly) + Django admin count of `PlatformEvent` rows with action `invite_issued`. Delivered = issued minus reported send failures. | Weekly | Operator |
| 2 | Invitation acceptance | Django admin: `PlatformEvent` count, action `invite_accepted`, over `invite_issued` for the same window. | Weekly | Operator |
| 3 | Verification success | Django admin: `AccessLog` rows for the email-confirmation route, success against total attempts. Corroborated by acceptance (row 2), since an unverified account cannot proceed. | Weekly | Operator |
| 4 | MFA completion | Django admin: `PlatformEvent` count, action `mfa_enrolled`, against the count of active memberships in `apps/tenants` admin. | Weekly | Operator |
| 5 | Reset completion | Django admin: `PlatformEvent` count, action `password_changed`, cross-read with `AccessLog` rows on the reset routes for attempts that did not complete. | Weekly | Operator |
| 6 | Upload success count | `AccessLog` table: POST requests to the document upload path, counted by `status_code`. Failures corroborated by Sentry storage-inconsistency reports. **No document admin exists**, so this is the only source — see the gap note below. | Weekly | Operator |
| 7 | Download success count | `AccessLog` table: GET requests to the document download path, counted by `status_code`. | Weekly | Operator |
| 8 | Support requests | Support notes: one line per inbound contact, with date, channel, and one-sentence topic. | Logged on arrival, tallied weekly | Support contact |
| 9 | Failed logins (aggregate) | Django admin: `PlatformEvent` count, action `login_failed`. **Aggregate only** — a per-account breakdown is a security review, not a metric, and is done from the runbook when an incident calls for it. | Weekly | Security contact |
| 10 | 4xx / 5xx rates | gunicorn access log: aggregate by `status=` field over the window. **Site-wide only — the log carries no URL**, so there is no per-endpoint breakdown. Sentry carries the exceptions behind the 5xx. | Weekly, and on any Sentry alert | Operator |
| 11 | Page latency | UptimeRobot response time for the monitored URLs. Corroborated in aggregate by the `duration=` field in the gunicorn access log — again **site-wide only, no per-endpoint split**. | Weekly (UptimeRobot reports continuously) | Operator |
| 12 | Accountant time-to-find-client | Stopwatch session with the pilot firm's accountant, timed by hand, result written to support notes. **Requires recorded participant consent** before the session starts. | Twice: once in pilot week 1, once in the final week | Operator |
| 13 | Accountant time-to-identify-overdue | Same stopwatch session as row 12, second task. **Requires recorded participant consent.** | Same two sessions | Operator |
| 14 | Workflows done outside app-mei | Support notes: asked in the stopwatch sessions and in the weekly check-in, written down as prose. No instrumentation. | Weekly check-in | Operator |
| 15 | Repeated feature requests | Support notes: a request is recorded each time it is raised; "repeated" means it appears in notes from more than one occasion. | Tallied weekly | Operator |
| 16 | Pilot-stopping defects | The stop-condition table in Part 2. Count of stop conditions triggered, each with its incident record. | On occurrence, reviewed weekly | Security contact |

### Consent for the stopwatch sessions

Rows 12 and 13 are the only metrics that observe a person working. Before either
session: state what is being timed, that no recording of the screen or voice is kept
beyond the timing and written notes, that the result is used to judge the product and
never the participant, and that they may stop at any point. Record that consent was
given — date, name, and what was agreed — in the support notes. No consent, no session,
no number.

### Metrics that cannot be sourced without new code

**None of the sixteen requires new code.** Two are weaker than they look and are stated
here rather than dressed up:

* **Rows 6 and 7 (upload/download counts)** have no dedicated audit action and no
  Django admin for the document model. `AuditAction` has no upload or download member,
  and `apps/obligations/models/documents.py` is not registered in any admin. The counts
  therefore come from `AccessLog` path-and-status queries, which are accurate for
  request outcomes but are bounded by the 180-day purge and are personal data. Adding a
  first-class counter would mean new code and new collection, so we did not.
* **Rows 10 and 11 (4xx/5xx, latency)** are site-wide, never per-endpoint, for the
  URL-free-log reason above. UptimeRobot gives per-monitored-URL latency, which is a
  handful of URLs, not full coverage.

Rows 1, 3, and 11 additionally depend on surfaces whose **accounts are not yet
created**: UptimeRobot (PILOT-207) and Healthchecks.io (PILOT-208) are the intended
sources and the endpoints they will watch are live, but nothing is collecting yet.
Neither may be reported as a measured number until those todos close.

## Part 2 — Stop conditions

Any one of these pauses invitations, triggers an assessment, and may trigger a
rollback to the previous image tag plus the restore procedure. There is no severity
ladder here on purpose: each of these is already the top of it.

| Stop condition | Detection signal | First action |
| --- | --- | --- |
| Cross-tenant or cross-client data exposure | A report from the firm or a portal user; an `Event` or `AccessLog` row showing a user reaching another tenant's or client's data; a `PlatformEvent` with action `all_tenants_access` outside a known operator session; a Sentry exception from an isolation control. | **Immediate stop.** Pause invitations, preserve logs, open an LGPD art. 48 assessment (`docs/lgpd.md` §4), notify the encarregado. Do not restart until scope is known. |
| Credential leak in any log or monitoring surface | A token, key, password, or session value found in container logs, the gunicorn access log, the `AccessLog` path column, or a Sentry event body. Found by the log grep in the walkthrough evidence, by a Sentry review, or by report. | Pause invitations. Rotate the leaked credential immediately. Assess exposure window from the log timestamps. Treat as an incident under `docs/lgpd.md` §4 if personal data was reachable with it. |
| Document loss or checksum mismatch | A download returning bytes whose sha256 does not match the stored checksum; a storage-inconsistency report in Sentry; a document row whose object is absent from the bucket. | Pause invitations and pause uploads. Do not delete anything. Attempt recovery from object-storage versioning, then from the off-host backup per `ops/RESTORE.md`. Record which documents were affected and inform the firm. |
| Email outage longer than 24 hours | Sentry reports of send failures, sustained; no delivery over a full day confirmed by a test send; invitations issued but never delivered. | Pause invitations (nothing will arrive anyway). Diagnose the provider, including suppression handling. Tell the firm before they discover it. Re-notify per the runbook once delivery is restored. |
| Full outage longer than 4 hours | UptimeRobot down alerts sustained past four hours, corroborated by `/healthz` and `/readyz` failing from the operator's own machine. | Pause invitations. Work the outage from the runbook. At the four-hour mark, decide explicitly between continuing to repair and rolling back to the previous image tag. |
| Unrecoverable backup failure longer than 48 hours | Healthchecks.io check silent or `/fail`-pinged for two consecutive nights; the freshness marker stale; the off-host copy absent from the bucket. | Pause invitations. A pilot without a recoverable backup does not continue. Run the backup by hand, and if it cannot be made to succeed, stop the pilot until it can. |
| The pilot firm requests a stop | They say so, by any channel. | Stop. Acknowledge the same day, pause invitations, ask what happened, and agree with them whether this is a pause or the end. Their data-handling choices, including erasure, follow `docs/retention.md`. |

### What "pause invitations" means concretely

Stop issuing new invitations and revoke any outstanding unused ones per the runbook's
invitation-revocation entry. Existing users keep working unless the condition itself
requires otherwise. The point is to stop widening the blast radius while the
assessment runs.

### Recording a stop

Every triggered stop condition gets a written record: what was seen, when, which signal
raised it, what was done first, and how it resolved. That record is what makes row 16
a number instead of a memory, and it is what a post-pilot review reads.

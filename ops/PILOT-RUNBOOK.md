<!-- required-sections: tenant-creation user-invitation mfa-recovery password-reset email-delivery-failure document-upload-failure storage-outage database-outage redis-outage stale-scheduler stale-backup low-disk sentry-alert-triage invitation-revocation user-deactivation tenant-data-removal pilot-exit-and-conversion incident-severity-ladder comms-channels business-hours rollback-steps evidence-retention lgpd-responsibilities production-access-list emergency-access-recording monitor-to-action -->

# Pilot runbook

The operating manual for one cooperative firm, 10-30 MEI clients, one real monthly
cycle. Every procedure below is derived from code and scripts that exist in this
repository at the commit you are reading. Where a procedure depends on an external
system that has not been created yet, the entry says so in place rather than implying
the system is standing.

## How to read this file

Each incident entry carries three fixed parts:

- **Detection signal** — the monitor, endpoint field, alert, or user report that
  surfaces the condition. If nothing surfaces it, the entry says so.
- **First response** — the single next action, not a menu.
- **Then** — the ordered remainder.

Commands are given exactly as they run. A command that does not appear in this file
does not exist as a supported procedure; do not improvise one during an incident.

### What does not exist yet

None of the following has been created, and no entry in this file may be read as a
claim that it has:

- No UptimeRobot account and no monitors: M1a, M1b, M1c, M1d, the M1n negative control,
  M2, M3 and M4 are all uncreated.
- No Healthchecks account, no check, and no `HC_URL` or `HC_VERIFY_URL` secret.
- Amazon SES is not configured for production sending; the account is not out of
  sandbox.
- There is no target VPS. Every `docker compose` command below presumes the production
  host that Wave 4 provisions.
- No procedure in this file has been rehearsed. Restore rehearsal timings in
  `ops/RESTORE.md` are recorded there as PENDING.

Entries that depend on those systems name the dependency in their detection signal.

### Fill these in — operator-supplied values

Every angle-bracket token below is the operator's to supply. They are deliberately
not guessed, because a plausible-looking name is exactly what a later reader mistakes
for a decision that was made.

| Token | What it is |
| --- | --- |
| `<operator-name>` | Owner. Same person may hold all three roles, but must be named. |
| `<support-contact-name>` | Support contact. |
| `<security-contact-name>` | Security contact. |
| `<operator-email>` | Where the operator is reached. |
| `<support-channel>` | The channel the pilot firm writes to. |
| `<escalation-channel>` | The channel used when the support channel is not enough. |
| `<business-hours>` | Local working hours, with timezone. |
| `<out-of-hours-expectation>` | What the firm may expect outside those hours. |
| `<encarregado-mailbox>` | The LGPD encarregado mailbox, per `docs/lgpd.md`. |
| `<pilot-slug>` | The pilot firm's subdomain slug. |
| `<production-host>` | The production host, once Wave 4 provisions it. |
| `<region>` | The AWS region SES and the CLI commands run against. |
| `<production-access-list>` | The named humans in the production access table. |

---

## Monitor-to-action mapping {#monitor-to-action}

This is the index. Every row points at the entry that owns the procedure. No monitor
below exists yet; the rows describe what each will surface once PILOT-207 and
PILOT-208 are activated by the operator.

| Monitor | Signal | Entry that owns it |
| --- | --- | --- |
| UptimeRobot M1a — public `/healthz` | keyword `"status": "ok"` absent | Whichever of M1b–M1d is also DOWN |
| UptimeRobot M1b — public `/healthz` | keyword `"scheduler": "alive"` absent | [stale-scheduler](#stale-scheduler) |
| UptimeRobot M1c — public `/healthz` | keyword `"backup": "fresh"` absent | [stale-backup](#stale-backup) |
| UptimeRobot M1d — public `/healthz` | keyword `"disk": "ok"` absent | [low-disk](#low-disk) — **unless M1b is also DOWN**, in which case `disk` reads `unknown` because the stalled scheduler stopped refreshing the reading, and the fault is [stale-scheduler](#stale-scheduler) |
| UptimeRobot M1n — public `/healthz` | **DOWN is the expected state.** Reporting UP means keyword matching is not happening and M1a–M1d prove nothing | Nothing to action while DOWN; investigate the monitors themselves if UP |
| UptimeRobot M2 — public `/readyz` | `database` is `error` | [database-outage](#database-outage) |
| UptimeRobot M2 — public `/readyz` | `redis` is `error` | [redis-outage](#redis-outage) |
| UptimeRobot M3 — public `/versionz` | `release` is not a lowercase 40-hex SHA | [rollback-steps](#rollback-steps) |
| UptimeRobot M4 — pilot portal `/healthz` | Any M1a–M1d keyword absent, portal host only | The matching M1 entry, scoped to the portal host |
| Scheduled `verify-live` workflow | Per-field JSON contract failure, a `/healthz` keyword fragment missing from the live body, live release differs from `main`, TLS under 14 days, or the daily ping is absent | [rollback-steps](#rollback-steps) for release drift; TLS renewal is in `ops/README.md` |
| Sentry — Celery worker/beat | An unhandled task exception reaches Sentry | [sentry-alert-triage](#sentry-alert-triage) |
| Sentry — web | Invitation, portal upload, LGPD notification, or rate-limit-store exception | The matching entry below; triage path is [sentry-alert-triage](#sentry-alert-triage) |

The `/healthz` fields are produced at `apps/core/views.py`; `/readyz` and `/versionz`
are in the same module. `/healthz` deliberately does **not** return 503 for a stale
backup or low disk — it reports them in the body and stays 200, because flapping the
container healthcheck would cause a worse outage than the one being reported. Read
the body, not the status code, for those three fields.

**M1 is four monitors, not one, because UptimeRobot cannot evaluate JSONPath.** Its only
body-inspection primitive is a plain substring match, so each field gets its own keyword
monitor and a fifth impossible keyword sits permanently DOWN to prove matching is
happening at all. The keywords carry a space after the colon exactly as `JsonResponse`
renders them; the configuration and the reason are in
[`ops/README.md`](README.md), "External uptime monitoring: four keyword monitors and a
negative control". The semantic per-field assertions live in the scheduled `verify-live`
workflow, which has `jq`.

---

## Tenant creation {#tenant-creation}

**Detection signal** — not an incident. This is a planned action, requested through
`<support-channel>`.

**First response** — confirm in writing that the firm has authorized the tenant and
that no production data of a real firm is being imported without written
authorization.

**Then:**

1. Sign in to the Django admin as a `is_staff` account from the
   [production access list](#production-access-list). That account is required to
   carry MFA — `apps/accounts/mfa.py` puts `is_staff` in the MFA population as a
   union with firm membership, precisely because a platform operator may hold no
   `Membership` at all.
2. Create the firm under **Tenants → Tenant**. `apps/tenants/admin.py` registers it
   with `slug`, `name`, `plan`, `is_active`. The slug is the subdomain the firm is
   reached at and is not cosmetic.
3. Verify the tenant is reachable at its host before inviting anyone. A tenant that
   resolves nowhere produces invitation links that go to a dead door.
4. Record the creation in the pilot notes with date and requester.

**Do not** create memberships by hand in the admin as the normal path. Membership is
built by invitation acceptance from `invite.tenant_id`; see
[user-invitation](#user-invitation).

---

## User invitation {#user-invitation}

**Detection signal** — not an incident. Requested through `<support-channel>`.

**First response** — confirm the requester is entitled to add that person to that
firm.

**Then:**

1. Issue the invitation from the firm's team page (firm side) or the portal invite
   path (client side). Both routes land in `apps/accounts/views.py`.
2. The invitee receives a link. Acceptance is single-use and locked:
   `apps/accounts/invites.py` refuses an invitation that already carries
   `accepted_at`, that carries `revoked_at`, or whose `expires_at` has passed, and it
   takes a row lock before stamping `accepted_at`.
3. Membership is created with `update_or_create`, not `get_or_create`. That matters
   for re-hiring: a previously deactivated member already has a `(user, tenant, NULL)`
   row, and `get_or_create` would have consumed the token while leaving
   `is_active=False` — locking the account out with its one way back in already spent.
   Re-inviting a leaver is the supported path.
4. A portal invitation redeemed at the wrong firm's door is refused by
   `assert_portal_invite` on both the firm and the scope halves. If a user reports
   "the link says it is not for me", that refusal is the control working; reissue at
   the correct host.

**If the email never arrives**, do not reissue blindly — go to
[email-delivery-failure](#email-delivery-failure) first. Reissuing into a suppressed
address produces another silent non-delivery.

---

## MFA recovery {#mfa-recovery}

**Detection signal** — user reports they cannot complete the second factor. There is
no monitor for this; it arrives through `<support-channel>`.

**First response** — ask whether they still hold their recovery codes. Recovery codes
are an enabled authenticator type: `config/settings/base.py` sets
`MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]`.

**Then, in order:**

1. **Recovery codes path (preferred).** The user signs in with a recovery code, then
   re-enrols TOTP from their account page. No operator action, no identity risk.
2. **Operator reset path (only when codes are gone).** Verify identity out of band
   through a channel that is not the account's own email — an email-only verification
   proves control of the mailbox, which is the thing that may have been lost. Record
   who you verified and how, per
   [emergency-access-recording](#emergency-access-recording).
3. Remove the user's authenticators via the Django admin, then require immediate
   re-enrolment. The user is redirected to `mfa_activate_totp` on their next
   authenticated request: `apps/accounts/mfa.py` names that URL and exempts
   `/accounts/`, `/healthz`, and `/lgpd/` from the gate — `/accounts/` because the
   enrolment page and logout live there and guarding it would be a loop, `/lgpd/`
   because it is a statutory channel reachable without an account.
4. A user holding only recovery codes has no working second factor, merely a way back
   in — `apps/accounts/middleware.py` says so explicitly. Do not close the ticket
   until TOTP is re-enrolled.

**Never** disable the MFA requirement for a population to resolve a single account.

---

## Password reset {#password-reset}

**Detection signal** — user request through `<support-channel>`, or the user
self-serves. No monitor.

**First response** — direct the user to the self-service reset under
`/accounts/` (allauth's tree, mounted at `config/urls.py`). The operator does not set
passwords.

**Then:**

1. The reset mail goes through the same SMTP path as every other message. If it does
   not arrive, that is an email incident, not a password incident — see
   [email-delivery-failure](#email-delivery-failure).
2. A completed reset does not clear the MFA requirement. If the user also lost their
   second factor, run [mfa-recovery](#mfa-recovery) as a separate, separately
   recorded action.
3. Reset links are credentials. They must never be pasted into
   `<support-channel>`, a ticket, or a Sentry comment. The credential-canary suite
   exists because that class of leak is the one that does not announce itself.

---

## Email delivery failure {#email-delivery-failure}

**Detection signal** — three distinct surfaces, and they mean different things:

- **Sentry, web.** An invitation send that raises `SMTPException` or `OSError` is
  captured at `apps/accounts/views.py` by `_mark_invitation_delivery_failure`, which
  also calls `transaction.set_rollback(True)` and puts a form error in front of the
  user. The invitation row is rolled back — nothing half-issued survives.
- **Sentry, LGPD.** `apps/lgpd/views.py` captures notification failures through
  `_capture_exception_safely` and records `notification_last_error` on the row.
- **User report.** "It says sent but nothing arrived." This is the SES suppression
  symptom and produces no exception at all.

**First response** — determine which of the three you have. A silent non-delivery and
a raised `SMTPException` have opposite causes and opposite fixes.

### Raised SMTP error

1. The user already saw an honest error page; the invitation was not created. Do not
   tell them it was sent.
2. Read the Sentry event. Credential scrubbing is verified by
   `tests/security/test_credential_log_hygiene.py`; if you see a raw token, key, or
   password in an event, that is a stop condition — see
   [incident-severity-ladder](#incident-severity-ladder).
3. Check SES configuration and credentials against the SES section of `ops/README.md`.
4. Reissue the invitation once the send path works. Reissue means **issue a new
   invitation**, not resend the old one — see [resend by reissue](#resend-by-reissue)
   below.

### Silent non-delivery: SES suppression handling

SES keeps an account-level suppression list, and an address on it is silently not
delivered to. The full procedure and its judgement call live in `ops/README.md` under
the suppression heading; the commands are:

```sh
aws sesv2 get-suppressed-destination --email-address <address> --region <region>
aws sesv2 list-suppressed-destinations --region <region>
aws sesv2 delete-suppressed-destination --email-address <address> --region <region>
```

Removing an address the recipient's provider genuinely rejected re-earns the bounce
and damages the domain's reputation. Removal is a judgement call and is recorded with
a reason.

### `lgpd_renotify` usage

When an LGPD data-subject request was filed but the encarregado notification did not
land, the pending rows are retried by:

```sh
docker compose -f docker-compose.prod.yml exec web \
  python manage.py lgpd_renotify --batch-size 25
```

Facts that govern its use, read from
`apps/audit/management/commands/lgpd_renotify.py`:

- It lives under **`apps/audit/`**, not `apps/lgpd/`.
- It is **manual only**. No scheduler runs it. If nobody runs it, nothing is retried.
- It touches only rows where `encarregado_notified_at IS NULL`, so it is idempotent:
  a row already stamped is not re-notified and cannot be double-sent by re-running.
- `--batch-size` is bounded and validated; an out-of-range value raises
  `CommandError` before anything is sent.
- A row that fails again gets `notification_last_error` written and the command exits
  non-zero naming how many failed and how many were sent. A non-zero exit here means
  "some are still pending", not "nothing worked".

Run it **after** the send path is fixed, never as a way to work around a broken one.

### Resend by reissue, and revoke {#resend-by-reissue}

There is no resend. An invitation token is issued once and stored as a digest; the
raw token exists only in the message that was sent. To get a working link to someone:

1. Revoke the outstanding invitation — see
   [invitation-revocation](#invitation-revocation).
2. Issue a new one.

Leaving the old invitation live while issuing a new one means two valid tokens for one
seat, and revocation of the wrong one leaves the other standing.

### Why opening a link does not consume it

Support will be asked this: "I clicked the link from my mail client and now it says
already used." That is not what happens, and the distinction matters when you are
diagnosing.

Acceptance is a **POST under a row lock**. `apps/accounts/invites.py` stamps
`accepted_at` only on the locked row inside the acceptance path. A GET renders the
acceptance page and nothing more — `assert_portal_invite` exists precisely because a
"wrong-door GET renders before domain acceptance can revalidate the scope under the
row lock". So:

- A mail-scanner or link-prefetcher that issues a **GET** on the invitation URL does
  **not** consume the invitation.
- A user who opens the link, closes the tab, and opens it again still has a redeemable
  invitation.
- "Already used" means `accepted_at` is genuinely set — somebody completed the POST.
  Treat that as a real acceptance and find out who, do not reissue reflexively.

### Standing rule: `sentry-sdk` upgrades

**Any upgrade of `sentry-sdk` re-runs the credential-canary suite before it merges:**

```sh
uv run pytest tests/security/test_credential_log_hygiene.py -q
```

That suite pins the `before_send` scrubber, the event-scrubber denylist extension, the
gunicorn and Caddy log formats, the production console filter, and the real-integration
control that proves the trace could have seen a planted canary. Scrubbing is
SDK-version-coupled behaviour; an upgrade that quietly changes it is exactly the
regression this pilot cannot absorb.

---

## Document upload and storage failure {#document-upload-failure}

**Detection signal** — Sentry event from `apps/portal/views.py`, which calls
`sentry_sdk.capture_exception(error)` on the upload path; plus a user report from the
portal.

**First response** — confirm whether the object store is reachable at all. If it is
not, this is [storage-outage](#storage-outage), and individual upload triage is
premature.

**Then:**

1. Ask the user to retry once. A single transient PUT failure is not an incident.
2. If the row exists but the object does not, or bytes disagree, run the
   reconciliation report:

   ```sh
   docker compose -f docker-compose.prod.yml exec web \
     python manage.py reconcile_documents --hash-sample-size 5
   ```

   `apps/core/management/commands/reconcile_documents.py` is
   **report-only by construction** — its help string says "Report document row/object
   inconsistencies without modifying storage" and it prints `COMPLETE report-only`. It
   has **no `--dry-run` flag**; passing one will error. Some planning text says
   otherwise; the plan is wrong and the command is right.
3. `--hash-sample-size` is the bounded per-tenant authenticated-read sample and is
   validated against a maximum. It defaults to a small number deliberately: this
   command reads real document bytes.
4. Document loss or a checksum mismatch is a **stop condition**. Do not continue
   inviting users; go to [incident-severity-ladder](#incident-severity-ladder).

---

## Storage outage {#storage-outage}

**Detection signal** — no dedicated monitor exists. It surfaces as a cluster of
upload/download failures in Sentry from `apps/portal/views.py`, and through user
reports. `/healthz` and `/readyz` do **not** check object storage, and will both stay
green through a total bucket outage. Know that before you trust a green dashboard
during a storage incident.

**First response** — prove reachability with the probe rather than by guessing:

```sh
docker compose -f docker-compose.prod.yml exec web \
  python manage.py storage_probe
```

`apps/core/management/commands/storage_probe.py` verifies private storage "by write,
read, refusal, and delete effects" — it writes a probe object, reads it back
authenticated, asserts anonymous GETs are refused, and deletes it. A pass means
credentials, network, bucket policy, and the refusal control are all intact.

**Then:**

1. If the probe fails, the fault is below the application. Check provider status and
   the bucket configuration in `ops/README.md` (object storage: versioning, lifecycle,
   probe).
2. Tell the pilot firm in `<support-channel>` that document upload and download are
   unavailable, and that everything else works. Partial availability that nobody
   explained reads as a total outage.
3. Do not disable the anonymous-refusal control to "restore access". The probe asserts
   that refusal for a reason.
4. When storage returns, run `reconcile_documents` as in
   [document-upload-failure](#document-upload-failure) to find rows whose objects
   never landed.

---

## Database outage {#database-outage}

**Detection signal** — `/readyz` returns `database: error` and HTTP 503 (UptimeRobot
M2). `/readyz` runs `SELECT 1` under `@transaction.non_atomic_requests` and catches
`DatabaseError` at exactly that boundary, so `database: error` is a real connection or
query failure, not a broad swallow.

Note the asymmetry: `/healthz` catches `DatabaseError` around the heartbeat read and
still returns `status: ok` with `scheduler: unknown`, `backup: unknown`,
`disk: unknown`. **`/healthz` staying 200 during a database outage is by design.**
`/readyz` is the dependency signal; do not use `/healthz` to decide whether the
database is up.

**First response** — check the database container's health on `<production-host>`:

```sh
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail 200 db
```

**Then:**

1. If the container is unhealthy but the data is intact, restart it and re-check
   `/readyz`.
2. If the data is not intact, this is a restore. Follow `ops/RESTORE.md`, which now
   carries the ownership-pinned procedure — `createdb -O app_migrator` and
   `pg_restore --role=app_migrator`, with `ops/sql/roles.sql` applied first. Do not
   improvise a restore during an incident; the ownership pinning is what stops the
   restored database from having the wrong owners and silently breaking RLS.
3. A full outage exceeding 4 hours is a stop condition. See
   [incident-severity-ladder](#incident-severity-ladder).
4. Note that the backup script refuses to run against an unhealthy database
   (`ops/backup.sh` exits 1 with "db container is not healthy"). A database incident
   therefore tends to produce a stale-backup signal the following morning; that is
   consequence, not a second fault.

---

## Redis outage {#redis-outage}

**Detection signal** — `/readyz` returns `redis: error` and HTTP 503 (UptimeRobot M2).
`/readyz` sets and reads back a cache key and catches `InvalidCacheBackendError` and
`RedisError`. **This is the signal.** There is no other reliable one, because of what
the application does next.

**What users experience: a site-wide 429, not a 500.**

`apps/security/ratelimit.py` `exceeds()` wraps the rate-limit counter and catches both
`redis.exceptions.ConnectionError` and the builtin `ConnectionError` — they are
distinct types; the redis one descends from `RedisError`, not from `OSError`. On
catch it calls `sentry_sdk.capture_exception()`, logs at ERROR, and **returns `True`**,
meaning "this request just overflowed the bucket". The request is denied with 429.

Because the authenticated limit runs on every authenticated request, a Redis outage
therefore denies **every authenticated request with HTTP 429**. Consequences you must
hold in mind during triage:

- The firm reports "rate limited" or "too many requests", not "the site is down". They
  are describing a Redis outage.
- The application is not erroring. You will not find a flood of 500s.
- Sentry fills with the same captured connection exception at a very high rate.
- Fail-closed is deliberate. A rate limiter that fails open removes the control at
  precisely the moment it cannot be observed.

**First response** — confirm with `/readyz`, then check the Redis container:

```sh
curl -s https://<pilot-slug>.samaronefialho.dev/readyz
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail 200 redis
```

**Then:**

1. Restore Redis. Recovery is automatic — `exceeds()` resumes counting normally as
   soon as the connection succeeds; nothing needs to be reset or flushed.
2. Tell the firm in `<support-channel>` what the 429 meant. "You were rate limited"
   with no explanation trains people to retry harder.
3. Do **not** disable the rate limiter to clear the 429s. That converts an
   availability incident into a security incident.

---

## Stale scheduler {#stale-scheduler}

**Detection signal** — `/healthz` reports `scheduler: stale` and, uniquely among the
`/healthz` fields, returns HTTP 503 (UptimeRobot M1). A stale scheduler is the one
condition `/healthz` escalates, because a process that is not running scheduled work is
not serving its purpose.

**First response** — check Celery beat and the worker on `<production-host>`:

```sh
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail 200 worker beat
```

**Then:**

1. Restart beat if it is down; confirm `/healthz` returns `scheduler: alive`.
2. If beat is up but the heartbeat is stale, the tasks are failing. Unhandled task
   exceptions reach Sentry through `CeleryIntegration` — go to
   [sentry-alert-triage](#sentry-alert-triage).
3. If `scheduler` reads `unknown` rather than `stale`, the heartbeat read itself hit a
   `DatabaseError`. That is a database incident wearing a scheduler mask; go to
   [database-outage](#database-outage).

---

## Stale backup {#stale-backup}

**Detection signal** — `/healthz` reports `backup: stale` while still returning HTTP
200 (UptimeRobot M1 must alert on the body field, not on the status code). If the
Healthchecks dead-man is activated by the operator, an absent daily ping is a second,
earlier signal.

**First response** — read the backup's exit code from the host. `ops/backup.sh` has an
explicit exit contract, and each code names a different failure:

| Exit | Meaning |
| --- | --- |
| 0 | Success. Marker written, off-host copy done (or not configured). |
| 1 | Precondition failed — unhealthy DB, missing `POSTGRES_USER`/`POSTGRES_DB`, WAL archiver failures, stalled archiver, or ownership of `/backups` could not be asserted. Nothing was backed up. |
| 2 | The backup itself failed — including a logical dump under 10 KiB, or a dump `pg_restore` cannot read. |
| 3 | Pruning failed — the oldest retained base is unreadable or carries no START WAL LOCATION, so there is no safe prune anchor. |
| 4 | The freshness marker could not be recorded. **The backup itself succeeded.** |
| 5 | The off-host copy failed. |

**The exit-5 / `backup=fresh` divergence — read this before you act.**

Exit 5 means the **local backup succeeded**. The marker was written before the
off-host step runs, deliberately: marker semantics are "the local backup is complete",
and a remote failure must not un-stamp a local success. So on an exit 5 you will see,
simultaneously:

- systemd records the unit as **failed** (exit 5);
- `/healthz` reports **`backup: fresh`**, and it is telling the truth;
- there is a local backup you can restore from **today**;
- there is **no off-host copy**, so a total host loss right now loses everything since
  the last successful off-host copy.

That divergence is a real operator trap in both directions. Treating exit 5 as "the
backup failed" leads to a panic re-run of a backup that already succeeded. Treating
`backup: fresh` as "we are covered" leads to believing you have host-loss protection
you do not have. Exit 5 is: **local recovery is fine, host-loss recovery is degraded.**
Fix the destination — `OFFHOST_S3_BUCKET`, `OFFHOST_S3_ENDPOINT`, `OFFHOST_S3_REGION`,
the dedicated access and secret keys, the AWS config that enforces path-style SigV4,
and the presence of the `aws` CLI are each checked separately and each exit 5 with
their own message.

Exit 4 has the mirror shape: the backup succeeded and only the marker is missing, so
`/healthz` will read `stale` while a good backup exists on disk.

**Then:**

1. Fix the cause named by the exit code, then run the backup manually and confirm exit
   0 and `backup: fresh`.
2. An unrecoverable backup failure lasting more than 48 hours is a stop condition.
3. Off-host copies are inert until the operator sets `OFFHOST_S3_BUCKET`; until then
   an absent off-host copy is configuration state, not an incident.

---

## Low disk {#low-disk}

**Detection signal** — `/healthz` reports `disk` as something other than `ok`, at HTTP
200 (UptimeRobot M1, body field). The value comes from a `DiskHeadroom` enum, and
`unknown` means the heartbeat read failed rather than that the disk is fine.

**First response** — do not delete anything yet. Find what grew:

```sh
df -h
docker system df
du -sh /backups/* | sort -h | tail
```

**Then:**

1. The backup tree and the WAL archive are the two things that grow on schedule. Both
   are bounded by the retention and prune logic in `ops/backup.sh`; the prune anchor is
   the **oldest retained base**, so deleting bases by hand can strand WAL and break the
   anchor. Do not hand-prune the WAL archive.
2. Host log rotation is configured; the procedure and the flood test are in
   `ops/README.md` under host log rotation.
3. `/healthz` deliberately does not 503 on low disk. The site is up. Fix the headroom
   before it becomes a backup failure — a full disk turns into exit 2 or exit 3 on the
   next nightly run.

---

## Sentry alert triage {#sentry-alert-triage}

**Detection signal** — a Sentry issue. The capture sites that exist today are:

| Site | What it means |
| --- | --- |
| `apps/accounts/views.py` | Invitation send failed; the row was rolled back and the user saw an error. |
| `apps/lgpd/views.py` | Encarregado notification failed; `notification_last_error` was recorded. |
| `apps/portal/views.py` | Portal document operation failed. |
| `apps/security/ratelimit.py` | Rate-limit store unreachable — almost always [redis-outage](#redis-outage). |
| Celery worker/beat | Unhandled task exception, via `CeleryIntegration`. |

**First response** — classify by capture site using the table above, then open the
matching entry. Do not start reading stack traces before you know which of five
different incidents you are in.

**Then:**

1. **Check the event for credentials before sharing it anywhere.** Scrubbing is
   enforced by `tests/security/test_credential_log_hygiene.py`, which covers the
   `before_send` scrubber, the recursive event-scrubber denylist, and a real-event
   control. If a raw token, key, or password is visible in an event, that is a
   credential leak in a monitoring surface and a **stop condition** — go to
   [incident-severity-ladder](#incident-severity-ladder) immediately and do not paste
   the event into `<support-channel>`.
2. A high-rate identical exception is usually infrastructure, not a bug. Check
   `/readyz` first.
3. Any `sentry-sdk` upgrade re-runs the canary suite — see
   [email-delivery-failure](#email-delivery-failure) for the standing rule.

---

## Invitation revocation {#invitation-revocation}

**Detection signal** — request through `<support-channel>`, or a decision made during
an incident (wrong recipient, suspected interception, seat no longer needed).

**First response** — revoke before doing anything else. An invitation that is still
redeemable while you deliberate is an open door.

**Then:**

1. Revoke via the firm's team page; the view is `invite_revoke_view` in
   `apps/accounts/views.py`.
2. Revocation takes a row lock and writes `revoked_at` with
   `save(update_fields=["revoked_at"])` — a deliberately narrow write, because saving
   the whole instance would rewrite every column from a read taken before the lock.
3. After revocation the token is dead. `apps/accounts/invites.py` refuses any
   invitation carrying `revoked_at`.
4. Browse pending invitations under **Tenants → Invite** in the admin. The `token`
   column is excluded from the admin everywhere: it holds a digest, but showing even
   the digest leaks enough to correlate an invitation across systems. Do not add it.
5. If the seat is still needed, issue a fresh invitation now — see
   [resend by reissue](#resend-by-reissue).

---

## User deactivation {#user-deactivation}

**Detection signal** — request through `<support-channel>` (a leaver), or a security
decision.

**First response** — deactivate the **membership**, not the user account, unless the
account itself is compromised.

**Then:**

1. In the admin under **Tenants → Membership**, clear `is_active`. Membership rows
   carry `user`, `tenant`, `client`, role, and `is_active`; `client` is displayed and
   filterable because it is the only thing distinguishing a firm-side membership from
   a portal identity, and two rows for the same user and tenant otherwise render
   identically.
2. Firm-side lookups filter on `is_active`, so a deactivated membership resolves
   nothing in that firm immediately.
3. Revoke any outstanding invitations for that person — see
   [invitation-revocation](#invitation-revocation).
4. Deactivation is reversible by re-invitation, and that path is supported on purpose
   (see [user-invitation](#user-invitation)). Do not delete the membership row to
   "clean up": deletion loses the audit trail, and the re-invite path is built around
   the row existing.
5. Deactivation is **not** erasure. A data-removal request is
   [tenant-data-removal](#tenant-data-removal).

---

## Tenant data removal {#tenant-data-removal}

**Detection signal** — a data-subject request through the `/lgpd/` channel, or a
written request from the firm. `/lgpd/` is reachable without an account and is exempt
from the MFA gate for exactly that reason.

**First response** — do not delete anything. Read `docs/lgpd.md` §5 (erasure requests
and why some data cannot be deleted) and `docs/retention.md` before taking any
action. Those two documents are authoritative and are deliberately not restated here;
a runbook copy would drift from the legal position it is supposed to reflect.

**Then:**

1. Confirm the encarregado has been notified. `docs/lgpd.md` §3 names the role. If the
   notification did not land, retry with `lgpd_renotify` — see
   [email-delivery-failure](#email-delivery-failure) for the command and its
   idempotency guarantee.
2. Determine which categories are erasable and which are retained under a legal basis.
   `docs/retention.md` sets the regimes, including why the access log is a separate
   table with its own rules.
3. Execute only what the two documents permit, and record what was done, by whom, and
   under which basis.
4. Deactivating a tenant (`is_active` on the Tenant row) removes access. It is not
   erasure and must never be reported as erasure.

---

## Pilot exit and conversion {#pilot-exit-and-conversion}

**Detection signal** — the pilot's fixed end, a stop condition that will not clear, or
the firm's request.

**First response** — freeze new invitations. Nothing is deleted at the moment of the
decision.

**Then, on conversion:**

1. Confirm in writing that the firm is continuing, and on what terms.
2. Re-verify the recovery track end to end before treating the tenant as production:
   `ops/RESTORE.md` for the database, and the object checks it references.
3. Carry forward the evidence set per [evidence-retention](#evidence-retention).

**Then, on exit:**

1. Confirm in writing what the firm wants done with its data, and record the answer.
2. Export anything the firm is entitled to take with it, before anything is removed.
3. Run [tenant-data-removal](#tenant-data-removal) for whatever the firm asks to have
   erased, subject to the retention regimes.
4. Deactivate the tenant and its memberships.
5. Record the exit reason honestly in the pilot notes, including which stop condition
   fired if one did.

---

## Incident severity ladder {#incident-severity-ladder}

**Detection signal** — this entry is the classifier every other entry escalates into.

**SEV-1 — stop conditions.** Any one of these pauses invitations immediately, is
assessed, and may trigger a rollback:

| Stop condition | Detection signal |
| --- | --- |
| Cross-tenant or cross-client data exposure | User report, or an access-log review. **Immediate stop plus an LGPD §48 assessment.** |
| Credential leak in any log or monitoring surface | Sentry event inspection, or the canary suite failing. |
| Document loss or checksum mismatch | `reconcile_documents` findings, or a user report. |
| Email outage over 24 hours | Sentry capture rate plus mailbox checks. |
| Full outage over 4 hours | `/readyz` and `/healthz` down continuously. |
| Unrecoverable backup failure over 48 hours | `/healthz` `backup: stale` persisting, plus the `ops/backup.sh` exit code. |
| The pilot firm asks to stop | `<support-channel>`. |

**First response for any SEV-1** — pause invitations, notify `<operator-name>` and
`<security-contact-name>` on `<escalation-channel>`, and write down the time. Assess
before you remediate; a remediation that destroys the evidence makes the LGPD
assessment impossible.

**SEV-2 — degraded, users affected, not a stop condition.** Redis outage, storage
outage, stale scheduler. Respond within `<business-hours>`, tell the firm what is
degraded and what still works.

**SEV-3 — single-user or cosmetic.** Password reset, MFA recovery, a single failed
upload. Normal support flow.

Escalation is one-way during an incident: a SEV-3 that turns out to be a leak is a
SEV-1 from the moment you know, not from the moment it is convenient.

---

## Communications channels {#comms-channels}

- **Firm to support:** `<support-channel>`, monitored during `<business-hours>`.
- **Support to firm:** the same channel, so the firm has one thread.
- **Escalation:** `<escalation-channel>`, used for SEV-1 and SEV-2.
- **Statutory:** the `/lgpd/` channel and `<encarregado-mailbox>`, per `docs/lgpd.md`.
  Statutory requests are never handled informally in `<support-channel>`.

Rules that hold on every channel:

1. **Never** paste an invitation link, reset link, recovery code, token, or Sentry
   event body into any channel. Describe, do not quote.
2. During a SEV-1, state what is known, what is not, and when the next update comes.
   An update that promises nothing is still an update.
3. A monitor that is knowingly red gets a written explanation wherever the alert lands.
   A check that is red and unexplained is a check people learn to ignore.

---

## Business hours and response expectations {#business-hours}

- **Business hours:** `<business-hours>`.
- **In hours:** `<support-contact-name>` acknowledges on `<support-channel>`.
- **Out of hours:** `<out-of-hours-expectation>`.
- **On-call:** there is no formal on-call rota for the pilot unless
  `<operator-name>` establishes one. Do not imply 24/7 coverage to the firm.

Set these expectations with the firm in writing at pilot start. The commonest pilot
failure is not an outage; it is an outage the firm thought someone was watching.

---

## Rollback steps {#rollback-steps}

**Detection signal** — a bad release: `/versionz` disagreeing with `main`, a SEV-1
traced to a deploy, or the `verify-live` workflow going red after a rollout.

**First response** — decide which of two rollbacks you need. They are different
procedures with different risks:

- **Bad image** → roll back to `:previous`. Procedure in `ops/README.md` under
  rollback.
- **Bad data** → restore. Procedure in `ops/RESTORE.md`.

**Then:**

1. Confirm the target: the previous image tag, plus the restore reference and the
   v0.2.0-rc1 notes.
2. Execute the rollback per `ops/README.md`; it also documents the hard limit —
   image rollback cannot undo a migration, so a release that migrated is a restore,
   not a tag flip.
3. **Expect `/versionz` to report the older SHA, and expect that to make things red.**
   After a `:previous` rollback the box is intentionally running an older commit.
   `verify-live` compares `/versionz` against the head of `main` and turns red for as
   long as the rollback stands; re-running the deploy job's assertion 5 by hand fails
   for the same reason. **That is the check working.** Do not "fix" it by editing the
   check or by re-deploying the bad image. If the rollback will stand for more than a
   short window, say so on `<escalation-channel>` — see
   [comms-channels](#comms-channels).
4. Let the red clear when the roll-forward lands.
5. Record the rollback, its reason, and its duration in the evidence set.

---

## Evidence retention {#evidence-retention}

- Pilot evidence lives in `.evidence/` as `PILOT-###-happy.txt`,
  `PILOT-###-failure.txt`, `PILOT-###-red.txt`, and `PILOT-###-operator.txt`.
- **Operator evidence contains no secrets.** Hostnames, IDs, PASS/FAIL lines, and
  timestamps only.
- Chronology must stay internally consistent: a red run precedes the happy run it was
  the red state for.
- Incident records keep: detection signal, timestamps, severity, actions taken,
  who took them, and outcome. They do not keep credentials, tokens, or the bodies of
  Sentry events.
- Access-log retention is governed by `docs/retention.md`, not by this file.
- Evidence is retained through the pilot and through
  [pilot-exit-and-conversion](#pilot-exit-and-conversion). It is not pruned to make a
  release look cleaner.

---

## LGPD responsibilities {#lgpd-responsibilities}

The authoritative documents are `docs/lgpd.md` (controller/operator split, lawful
bases, the encarregado, the incident runbook, and the erasure position) and
`docs/retention.md` (retention regimes). This section points at them and adds only the
pilot-specific operating facts.

- The **encarregado** is named in `docs/lgpd.md` §3 and is reached at
  `<encarregado-mailbox>`. `<security-contact-name>` is not automatically the
  encarregado; if they are the same person, say so explicitly.
- The rights channel is `/lgpd/`, reachable without an account and exempt from the MFA
  gate on purpose.
- Encarregado notification is retried with `lgpd_renotify` — see
  [email-delivery-failure](#email-delivery-failure). Nothing retries it automatically.
- A cross-tenant or cross-client exposure triggers the LGPD §48 assessment described in
  `docs/lgpd.md` §4, in addition to the SEV-1 response.
- Erasure is governed by `docs/lgpd.md` §5 and `docs/retention.md`. Do not answer a
  subject directly about what can be deleted without reading them.
- No additional personal or behavioural collection is added for the pilot. The metrics
  plan in `ops/PILOT-METRICS.md` is built entirely on existing surfaces for this
  reason.

---

## Production access list {#production-access-list}

`<production-access-list>` — the operator fills this table in. Every row must name a
real person; a row reading "the team" is not an access list.

| Name | Role | Host SSH | Django admin (`is_staff`) | Object storage | Sentry | SES console |
| --- | --- | --- | --- | --- | --- | --- |
| `<operator-name>` | Owner | | | | | |
| `<support-contact-name>` | Support | | | | | |
| `<security-contact-name>` | Security | | | | | |

Rules:

1. Every `is_staff` account carries MFA. That is not a policy statement here — it is
   enforced, and `apps/accounts/mfa.py` puts `is_staff` in the required population as a
   union with firm membership specifically because a platform operator may hold no
   membership at all.
2. Credentials are never shared between people. A shared credential makes
   [emergency-access-recording](#emergency-access-recording) meaningless.
3. Secret locations, not secret contents, are documented — the operator password
   manager, per the recovery-secret note in `ops/RESTORE.md`. No secret value belongs
   in this repository.
4. Review this table at pilot start, at any personnel change, and at
   [pilot-exit-and-conversion](#pilot-exit-and-conversion).
5. Rotation triggers are listed in `ops/README.md` under secrets.

---

## Emergency access recording {#emergency-access-recording}

**Detection signal** — none. This entry exists because emergency access is the one
privileged action nothing else observes, which is exactly why it must be written down
by the person who took it.

Record, at the time and not afterwards, whenever any of these happens:

- Someone outside `<production-access-list>` is granted access.
- Someone inside it uses access they do not normally use.
- A credential is used out of hours.
- An operator resets a user's MFA authenticators (see
  [mfa-recovery](#mfa-recovery)).
- Anyone reads production data outside a named support request.

Each record carries:

1. Who — a named person, never a role.
2. When — start and end, with timezone.
3. What was accessed, and what was changed.
4. Why — the incident or request it belongs to.
5. How identity was verified, if the access was granted to someone out of band.
6. What was revoked afterwards, and when.

Records go into the evidence set per [evidence-retention](#evidence-retention) and are
reviewed by `<security-contact-name>`. An emergency access that was never recorded is
indistinguishable, later, from an intrusion.

---

## Related documents

- `ops/README.md` — the exercised mechanisms: roles, RLS, rate limits, credential log
  hygiene, backups, deploy, rollback, secrets, SES, object storage, host log rotation,
  host requirements, the two-phase bootstrap, and decommissioning. It also carries the
  operator runsheets prepared but not yet executed.
- `ops/RESTORE.md` — database and object recovery, ownership-pinned.
- `ops/PILOT-METRICS.md` — pilot metrics, their sources, and the stop conditions with
  their detection signals.
- `docs/lgpd.md` — LGPD controller/operator split, encarregado, incident runbook,
  erasure.
- `docs/retention.md` — retention regimes.
- `docs/residual-risks-pilot.md` — what is accepted and still open.

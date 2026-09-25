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

Host commands run on the Lightsail production host, `<production-host>`, reached with
`ssh app-mei` from the operator's allow-listed address or through Lightsail browser SSH.
Every host block below assumes this preamble, the same one the deploy script and
`ops/README.md` use:

```sh
cd /opt/app-mei
compose='docker compose --env-file .env.prod -f docker-compose.prod.yml'
```

The `--env-file` flag isn't optional. `docker-compose.prod.yml` requires variables such
as `${SECRET_KEY:?...}`, and their values live only in `.env.prod`.

### What is standing, and what is not

Standing, as the operator recorded it between 2026-09-21 and 2026-09-25:

- Production runs on the AWS Lightsail host. The old OCI compute instance is stopped and
  kept only as the deep-rollback anchor (`ops/README.md`, "Decommissioning the old
  host"). The documents bucket stays in OCI Object Storage under the keep-oci ruling.
- UptimeRobot: M1a to M1d and M2 to M4 read UP, and the M1n negative control reads DOWN
  as designed (`.evidence/PILOT2-406-operator.txt`). Every monitor mails the owner's
  alert mailbox.
- Healthchecks: `app-mei-backup-lightsail`, `app-mei-backup-offhost-lightsail` and
  `app-mei-verify-live` exist and read UP. Their ping URLs sit in the password manager,
  never in this file.
- Sentry: project `app-mei`, alert rule `issue-alert-to-owner`. A new issue in
  environment `production` mails the owner (`.evidence/PILOT2-107-operator.txt`).
- Resend sends from the verified `samaronefialho.dev` domain with SPF, DKIM and DMARC
  passing ("Email provider (Resend SMTP)" in `ops/README.md`). SES is history.
- Restore timings are measured: 6 minutes for Track L and 98 minutes for Track H
  (`ops/RESTORE.md`, "Recovery timing status").

Not standing yet:

- No real pilot tenant. `<pilot-slug>` has no value, and M4 watches the placeholder
  host `pilot-portal.samaronefialho.dev` until the tenant exists.
- No active `is_staff` account. The temporary administrator used for the mailbox
  journeys is inactive (`.evidence/PILOT2-106O-operator.txt`). Turning one on is
  recorded under [emergency-access-recording](#emergency-access-recording).
- The contact item named under [Roles and contacts](#roles-and-contacts) isn't
  recorded as created in any evidence file. Until the owner confirms it, the support
  channel is agreed in principle only.
- Rows 30 to 32 (`PILOT2-406`, `PILOT2-106O`, `PILOT2-504`) are operator observations
  awaiting independent review. No gate has approved them.
- Resend's daily-cap refusal has never been observed, and delivered mail has landed in
  spam, so inbox placement is unproven.
- M2's database and Redis cases were proven against local negative bodies only. No live
  database or Redis outage has been drilled, on purpose ("Non-goals" under "Alerting
  drills" in `ops/README.md`).

### Roles and contacts

The owner holds every operational role for the pilot. Roles appear here by name and
initials only. No email address or phone number belongs in this file: the contact list,
with each role's address and number, lives in Proton Pass, vault `AWS/SaaS`, item
`app-mei pilot contacts`.

| Role | Holder | Covers | Evidence |
| --- | --- | --- | --- |
| Operator | Owner, SL | Every provider account (Sentry, Resend, UptimeRobot, Healthchecks), deploys, rollback decisions | `.evidence/PILOT2-000-operator.txt` |
| On-call | Owner, SL | Receives every alert. Acknowledges within 15 minutes in business hours, best effort outside | `.evidence/PILOT2-000L-operator.txt`; acknowledged as `Owner-on-call` in `.evidence/PILOT2-504-operator.txt` |
| Support contact | Owner, SL | Answers the firm on the support channel, keeps the support notes | Single-owner model; no other person appears in any artifact |
| Security contact | Owner, SL | SEV-1 assessment, credential leaks, review of emergency access | Single-owner model; no other person appears in any artifact |
| Encarregado (DPO) | Designated by the owner. Row 41 recorded the designation and the published mailbox name, never the holder's identity, so the holder's initials stay in the contact item | The `/lgpd/` channel, data-subject requests, LGPD incidents | `.evidence/PILOT2-000L-operator.txt` |

| Contact | Value |
| --- | --- |
| Support channel | The support thread agreed with the firm in writing at pilot start. Address in the contact item |
| Escalation channel | The owner's alert mailbox, the same one UptimeRobot, Healthchecks and Sentry mail. Address in the contact item |
| Encarregado mailbox | Published on `/lgpd/`. The value is `LGPD_ENCARREGADO_EMAIL` in the host's `.env.prod`, copied in the contact item |
| Business hours | 08:00 to 18:00 America/Sao_Paulo, Monday to Friday |
| Out of hours | Best effort. No acknowledgement time is promised |

Two angle-bracket tokens remain, on purpose:

| Token | What it is |
| --- | --- |
| `<pilot-slug>` | The pilot firm's subdomain slug. No real tenant exists yet. |
| `<production-host>` | The Lightsail production host. Its address stays out of this file; `ssh app-mei` reaches it. |

---

## Monitor-to-action mapping {#monitor-to-action}

This is the index. Each row names the signal, the evidence that proves the signal
fires (or says it was never drilled), the entry that owns the response, and the first
command. Rows 30 to 32 of the pilot plan are the evidence: `PILOT2-406` for the monitor
inventory and backup checks, `PILOT2-106O` for the mail journeys, and `PILOT2-504` for
the alerting drills, all under `.evidence/`. Host commands assume the preamble above.

| Signal | Proven by | Owning entry | First command |
| --- | --- | --- | --- |
| UptimeRobot M1b, keyword `"scheduler": "alive"` absent on public `/healthz`. M1a, M1c and M1d go DOWN with it, because the 503 alone counts as down | Beat drill of 2026-09-24: `/healthz` 503 at 23:05:06 UTC, mail `Monitor-is-DOWN:M1b` at 23:09:30 (`PILOT2-504`) | [stale-scheduler](#stale-scheduler). **If M2 is DOWN too and M1a stays UP**, `/healthz` is answering 200 with `scheduler`, `backup` and `disk` all `unknown` because the heartbeat read failed, and the fault is [database-outage](#database-outage) | `curl -s -w '\n%{http_code}\n' https://samaronefialho.dev/healthz` |
| UptimeRobot M1a, keyword `"status": "ok"` absent | Went DOWN in both beat drills (`PILOT2-504`) | Whichever of M1b, M1c or M1d is also DOWN. With M2 and M4 DOWN too, the host is unreachable: [incident-severity-ladder](#incident-severity-ladder), full outage | `curl -s -w '\n%{http_code}\n' https://samaronefialho.dev/healthz` |
| UptimeRobot M1c, keyword `"backup": "fresh"` absent while M1b is UP | Keyword pinned by `tests/test_healthz_keyword_monitors.py`. A stale backup on its own has never been drilled live | [stale-backup](#stale-backup) | `journalctl -t app-mei-backup -n 50 --no-pager` |
| UptimeRobot M1d, keyword `"disk": "ok"` absent while M1b is UP | Keyword pinned by the same test. Low disk on its own has never been drilled; M1d went DOWN in the beat drills as part of the scheduler cascade | [low-disk](#low-disk). **If M1b is also DOWN**, `disk` reads `unknown` because the stalled scheduler stopped refreshing it, and the fault is [stale-scheduler](#stale-scheduler) | `df -h /` |
| UptimeRobot M1n reads **UP** | M1n read DOWN in the inventory of 2026-09-22 (`PILOT2-406`). DOWN is its required state | None while DOWN. If UP, keyword matching has stopped and M1a to M1d prove nothing: check the monitors themselves | Read the UptimeRobot monitor list |
| UptimeRobot M2, keyword `"database": "ok", "redis": "ok"` absent on public `/readyz` | Live body matched, and local negatives for each field wrong or missing failed as required (`PILOT2-406`, monitor contract check). No live outage drill | `database: error` is [database-outage](#database-outage); `redis: error` is [redis-outage](#redis-outage) | `curl -s -w '\n%{http_code}\n' https://samaronefialho.dev/readyz` |
| UptimeRobot M3, keyword `"release"` pinned to the full SHA of the last verified deploy, absent on public `/versionz` | Refreshed three times and read back UP, latest `92cf94a` on 2026-09-25 (`PILOT2-406`) | After an unannounced change or drift, [rollback-steps](#rollback-steps). After a deploy you made, update the pin to the new `/versionz` value | `curl -s https://samaronefialho.dev/versionz; git ls-remote origin refs/heads/main` |
| UptimeRobot M4, the four healthy `/healthz` fields as one literal, absent on the portal host | Live body matched, and eight local negatives failed as required (`PILOT2-406`). Still on the placeholder host | The matching M1 entry, scoped to the portal host | `curl -s -w '\n%{http_code}\n' https://pilot-portal.samaronefialho.dev/healthz` |
| Healthchecks `app-mei-backup-lightsail` DOWN, by silence or by a `/$rc` failure ping | Missed-run drill: mail `DOWN-app-mei-backup-lightsail` at 2026-09-25 08:04:08 UTC, both checks UP again at 08:05:00 (`PILOT2-504`) | [stale-backup](#stale-backup) | `systemctl list-timers app-mei-backup.timer --no-pager` |
| Healthchecks `app-mei-backup-offhost-lightsail` DOWN while the local check is UP | First scheduled nightly on 2026-09-23 exited 5; the off-host check went DOWN with status 5 and the local one stayed UP (`PILOT2-406`) | [stale-backup](#stale-backup), the exit-5 case | `journalctl -t app-mei-backup -n 50 --no-pager` |
| Healthchecks `app-mei-verify-live` DOWN, by silence or by the workflow's `/fail` ping | Scheduled run of 2026-09-23 sent its success ping and the check read UP (`PILOT2-406`) | [rollback-steps](#rollback-steps) for release drift; TLS renewal is in `ops/README.md` | `gh run list --workflow verify-live.yml -L 5` |
| Sentry rule `issue-alert-to-owner`: a new issue in `production` | Rule saved and read back 2026-09-21 (`PILOT2-107`). Events reached the project: a synthetic event at `699f764` with its password field `Filtered` (`PILOT2-504`), and `SMTPAuthenticationError` from `apps/accounts/views.py` in the wrong-SMTP control (`PILOT2-106O`). Receipt of the rule's alert mail was never recorded | [sentry-alert-triage](#sentry-alert-triage), which routes by capture site | Open the issue in Sentry; no host command first |
| Resend dashboard shows `Bounced`, or a user says nothing arrived | Bounce simulator produced `550 5.1.1`, status `Bounced`, suppression executed with no repeat send (`PILOT2-106O`). This surface raises no alert; it is read by hand | [email-delivery-failure](#email-delivery-failure) | Read the recipient's row in the Resend delivery log |

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

**Four simultaneous reds from M1 are one fault, not four.** A stale scheduler makes
`/healthz` answer 503, and UptimeRobot counts the 503 as down before it looks for any
keyword, so M1c goes DOWN even though the body still says `"backup": "fresh"`. The
beat drill of 2026-09-23 is what showed this. Chasing M1c or M1d during a scheduler
outage is the mistake to avoid.

---

## Tenant creation {#tenant-creation}

**Detection signal** — not an incident. This is a planned action, requested through
the support channel.

**First response** — confirm in writing that the firm has authorized the tenant and
that no production data of a real firm is being imported without written
authorization.

**Then:**

1. Sign in to the Django admin as a `is_staff` account from the
   [production access list](#production-access-list). That account is required to
   carry MFA — `apps/accounts/mfa.py` puts `is_staff` in the MFA population as a
   union with firm membership, precisely because a platform operator may hold no
   `Membership` at all. No `is_staff` account is active today, so turning one on is
   itself a record under [emergency-access-recording](#emergency-access-recording).
2. Create the firm under **Tenants → Tenant**. `apps/tenants/admin.py` registers it
   with `slug`, `name`, `plan`, `is_active`. The slug is the subdomain the firm is
   reached at and is not cosmetic.
3. Verify the tenant is reachable at its host before inviting anyone. A tenant that
   resolves nowhere produces invitation links that go to a dead door.
4. Repoint UptimeRobot M4 from the placeholder `pilot-portal.samaronefialho.dev` to
   the new tenant's portal host, then confirm it reads UP:

   ```sh
   curl -s -w '\n%{http_code}\n' https://<pilot-slug>-portal.samaronefialho.dev/healthz
   ```

5. Record the creation in the pilot notes with date and requester.

**Do not** create memberships by hand in the admin as the normal path. Membership is
built by invitation acceptance from `invite.tenant_id`; see
[user-invitation](#user-invitation).

---

## User invitation {#user-invitation}

**Detection signal** — not an incident. Requested through the support channel.

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

**Detection signal** — the user reports they can't complete the second factor. No
monitor sees this; it arrives through the support channel.

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

**Detection signal** — a user request through the support channel, or the user
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
   the support channel, a ticket, or a Sentry comment. The credential-canary suite
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
- **User report.** "It says sent but nothing arrived." Resend accepted the message and
  it never reached the inbox. This produces no exception at all.

The two Sentry surfaces mail the owner through the rule `issue-alert-to-owner`, on the
first occurrence only, because the rule fires on a new issue. The capture is proven: on
2026-09-22 a deliberately wrong SMTP password showed the user the honest error, created
no invitation row, and put `SMTPAuthenticationError` in Sentry
(`.evidence/PILOT2-106O-operator.txt`). Receipt of the rule's mail for that event wasn't
recorded. The user-report surface raises nothing; the Resend dashboard is read by hand.

**First response** — determine which of the three you have. A silent non-delivery and
a raised `SMTPException` have opposite causes and opposite fixes.

### Raised SMTP error

1. The user already saw an honest error page; the invitation was not created. Do not
   tell them it was sent.
2. Read the Sentry event. Credential scrubbing is verified by
   `tests/security/test_credential_log_hygiene.py`; if you see a raw token, key, or
   password in an event, that is a stop condition — see
   [incident-severity-ladder](#incident-severity-ladder).
3. Check the Resend configuration and credentials against "Email provider (Resend
   SMTP)" in `ops/README.md`. Two causes dominate: an expired or revoked API key in
   `EMAIL_HOST_PASSWORD`, and the free tier's 100-messages-per-day cap, which surfaces
   as a refused send rather than a silent drop. The dashboard's usage figure settles
   which of the two it is.
4. Reissue the invitation once the send path works. Reissue means **issue a new
   invitation**, not resend the old one — see [resend by reissue](#resend-by-reissue)
   below.

### Silent non-delivery: Resend delivery log and manual suppression

There is no webhook and no bounce feed into a mailbox. The Resend dashboard's delivery
log is the surface, and suppression is a human decision:

1. Find the recipient in the delivery log and read the event: delivered, bounced,
   complained, or never accepted.
2. Bounced or complained means **do not send again** until the address is confirmed
   good through a channel that is not that address. Record the date, the recipient, and
   the reason class in `.evidence/PILOT-105-operator.txt`.
3. Delivered, with the recipient insisting nothing arrived, is a recipient-side filing
   or spam-filter problem. Ask for the message-id and check their junk folder before
   touching anything here.

Re-sending to an address the receiving provider genuinely rejected re-earns the bounce
and spends domain reputation. The judgement call and its reason are written down.

### `lgpd_renotify` usage

When an LGPD data-subject request was filed but the encarregado notification did not
land, the pending rows are retried by the command below. Fix the send path first: if
the encarregado address is bouncing at Resend, or the daily cap is spent, every retry
fails the same way and writes the same error.

```sh
$compose exec web python manage.py lgpd_renotify --batch-size 25
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
`sentry_sdk.capture_exception(error)` on the upload path and mails the owner through
`issue-alert-to-owner` on a new issue; plus a user report from the portal. No drill has
fired this capture site on the Lightsail host.

**First response** — confirm whether the object store is reachable at all. If it is
not, this is [storage-outage](#storage-outage), and individual upload triage is
premature.

**Then:**

1. Ask the user to retry once. A single transient PUT failure is not an incident.
2. If the row exists but the object does not, or bytes disagree, run the
   reconciliation report:

   ```sh
   $compose exec web python manage.py reconcile_documents --hash-sample-size 5
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
green through a total bucket outage, and so will M1a to M1d, M2 and M4. Know that
before you trust a green dashboard during a storage incident. The bucket is the OCI
Object Storage documents bucket, kept there under the keep-oci ruling while the
application runs on Lightsail.

**First response** — prove reachability with the probe rather than by guessing:

```sh
$compose exec web python manage.py storage_probe
```

`apps/core/management/commands/storage_probe.py` verifies private storage "by write,
read, refusal, and delete effects" — it writes a probe object, reads it back
authenticated, asserts anonymous GETs are refused, and deletes it. A pass means
credentials, network, bucket policy, and the refusal control are all intact.

**Then:**

1. If the probe fails, the fault is below the application. Check OCI's service status
   and the bucket configuration in `ops/README.md` (object storage: versioning,
   lifecycle, probe).
2. Tell the pilot firm on the support channel that document upload and download are
   unavailable, and that everything else works. Partial availability that nobody
   explained reads as a total outage.
3. Do not disable the anonymous-refusal control to "restore access". The probe asserts
   that refusal for a reason.
4. When storage returns, run `reconcile_documents` as in
   [document-upload-failure](#document-upload-failure) to find rows whose objects
   never landed.

---

## Database outage {#database-outage}

**Detection signal** — `/readyz` returns `database: error` and HTTP 503, and UptimeRobot
M2 goes DOWN: its keyword `"database": "ok", "redis": "ok"` is absent. That keyword was
proven against a local `database: error` body, not a live outage
(`.evidence/PILOT2-406-operator.txt`). `/readyz` runs `SELECT 1` under the
`transaction.non_atomic_requests` decorator and catches `DatabaseError` at exactly that
boundary, so `database: error` is a real connection or query failure, not a broad
swallow.

Note the asymmetry: `/healthz` catches `DatabaseError` around the heartbeat read and
still returns `status: ok` with `scheduler: unknown`, `backup: unknown`,
`disk: unknown`. **`/healthz` staying 200 during a database outage is by design.** On
UptimeRobot that reads as M1b, M1c and M1d DOWN on their missing keywords while M1a
stays UP, next to M2 DOWN. That pattern is this entry, not three separate faults.
`/readyz` is the dependency signal; do not use `/healthz` to decide whether the
database is up.

**First response** — check the database container's health on `<production-host>`:

```sh
$compose ps
$compose logs --tail 200 db
```

**Then:**

1. If the container is unhealthy but the data is intact, restart it and re-check
   `/readyz`:

   ```sh
   $compose restart db
   curl -s -w '\n%{http_code}\n' https://samaronefialho.dev/readyz
   ```

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

**Detection signal** — `/readyz` returns `redis: error` and HTTP 503, and UptimeRobot
M2 goes DOWN on its absent keyword. As with the database case, the keyword was proven
against a local `redis: error` body only. The rate-limit capture below also mails the
owner through `issue-alert-to-owner`, once, on the first occurrence. `/readyz` sets
and reads back a cache key and catches `InvalidCacheBackendError` and `RedisError`.
**This is the signal.** There is no other reliable one, because of what the
application does next.

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
$compose ps
$compose logs --tail 200 redis
```

**Then:**

1. Restore Redis with `$compose restart redis`. Recovery is automatic: `exceeds()`
   resumes counting normally as soon as the connection succeeds, and nothing needs to
   be reset or flushed.
2. Tell the firm on the support channel what the 429 meant. "You were rate limited"
   with no explanation trains people to retry harder.
3. Do **not** disable the rate limiter to clear the 429s. That converts an
   availability incident into a security incident.

---

## Stale scheduler {#stale-scheduler}

**Detection signal** — `/healthz` reports `scheduler: stale` and, uniquely among the
`/healthz` fields, returns HTTP 503. UptimeRobot mails `Monitor-is-DOWN:M1b`, and M1a,
M1c and M1d go DOWN with it on the 503. The heartbeat goes stale 15 minutes after the
last tick. This path is drilled: on 2026-09-24 beat stopped at 22:50:28 UTC, `/healthz`
answered 503 at 23:05:06, and the M1b mail arrived at 23:09:30
(`.evidence/PILOT2-504-operator.txt`). A stale scheduler is the one condition
`/healthz` escalates, because a process that is not running scheduled work is not
serving its purpose.

During the outage `web` shows `unhealthy` in `ps`, because its healthcheck wants a 200
from `/healthz`, and Compose doesn't restart it. Firm pages keep answering 200. Don't
chase `web`; the fault is in `beat`.

**First response** — check Celery beat and the worker on `<production-host>`:

```sh
$compose ps
$compose logs --tail 200 worker beat
```

**Then:**

1. Start beat if it is down, then confirm `/healthz` returns `scheduler: alive`. Use
   `start`, not `up -d beat`: `up` waits for `web` to be healthy, and `web` stays
   unhealthy until the scheduler is back.

   ```sh
   $compose start beat
   curl -s -w '\n%{http_code}\n' https://samaronefialho.dev/healthz
   ```

2. If beat is up but the heartbeat is stale, the tasks are failing. Unhandled task
   exceptions reach Sentry through `CeleryIntegration` — go to
   [sentry-alert-triage](#sentry-alert-triage).
3. If `scheduler` reads `unknown` rather than `stale`, the heartbeat read itself hit a
   `DatabaseError`. That is a database incident wearing a scheduler mask; go to
   [database-outage](#database-outage).

---

## Stale backup {#stale-backup}

**Detection signal** — three signals, and the first two arrive first:

- **Healthchecks `app-mei-backup-lightsail`** goes DOWN when the nightly run fails
  (`/$rc` ping) or never pings at all. Its schedule is `0 3 * * *` America/Sao_Paulo
  with 2 hours of grace, so a silent night shows DOWN around 05:00 local time. Drilled
  on 2026-09-25: with the timer disabled, the mail `DOWN-app-mei-backup-lightsail`
  arrived at 08:04:08 UTC (`.evidence/PILOT2-504-operator.txt`).
- **Healthchecks `app-mei-backup-offhost-lightsail`** goes DOWN on its own when only
  the off-host copy fails. That happened for real on 2026-09-23: the first scheduled
  nightly exited 5, the off-host check went DOWN with status 5, and the local check
  stayed UP (`.evidence/PILOT2-406-operator.txt`).
- **UptimeRobot M1c** goes DOWN when `/healthz` reports `backup: stale` while still
  returning HTTP 200, because M1c alerts on the absent body keyword. The marker ages out
  after 36 hours, so this is the late signal.

**First response** — read the backup's exit code from the host:

```sh
systemctl list-timers app-mei-backup.timer --no-pager
systemctl status app-mei-backup.service --no-pager
journalctl -t app-mei-backup -n 50 --no-pager
```

`ops/backup.sh` has an explicit exit contract, and each code names a different failure:

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

1. Fix the cause named by the exit code, then run the backup by hand and confirm exit
   0, `backup: fresh`, and both Healthchecks checks back to UP:

   ```sh
   sudo systemctl start app-mei-backup.service
   journalctl -t app-mei-backup -n 50 --no-pager
   curl -s https://samaronefialho.dev/healthz
   ```

   On 2026-09-23 the exit-5 cause was a permissions fault: the service user couldn't
   read the AWS config directory. The fix and the recovery run are in
   `.evidence/PILOT2-406-operator.txt`.
2. If the timer was disabled, turn it back on with
   `sudo systemctl enable --now app-mei-backup.timer` and confirm `list-timers` shows
   a next run.
3. An unrecoverable backup failure lasting more than 48 hours is a stop condition.
4. The off-host copy is active on this host, so an exit 5 is an incident, not
   configuration state.

---

## Low disk {#low-disk}

**Detection signal** — `/healthz` reports `disk` as something other than `ok`, at HTTP
200, and UptimeRobot M1d goes DOWN on the absent `"disk": "ok"` keyword. Low disk on
its own has never been drilled; M1d has only gone DOWN as part of the stale-scheduler
cascade. The value comes from a `DiskHeadroom` enum, and `unknown` means the reading
is stale or the heartbeat read failed, not that the disk is fine. If M1b is DOWN too,
go to [stale-scheduler](#stale-scheduler) instead.

**First response** — do not delete anything yet. Find what grew:

```sh
df -h /
docker system df
sudo du -sh /opt/app-mei/ops/backups/* | sort -h | tail
```

`/backups` inside the `db` container is the host directory `/opt/app-mei/ops/backups`.

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

**Detection signal** — a mail from the Sentry rule `issue-alert-to-owner`: a new issue
was created in environment `production` in project `app-mei`
(`.evidence/PILOT2-107-operator.txt`). The rule fires on a **new** issue only. A second
occurrence of a known issue mails nobody, so during a high-rate failure the event count
on the open issue is the signal, not your inbox. A synthetic event on 2026-09-24 at
`699f764` reached the project with the right `release` and its password field
`Filtered` (`.evidence/PILOT2-504-operator.txt`). No evidence file records the rule's
mail arriving, so the delivery link of this chain is still unproven.

The capture sites that exist today are:

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
   the event into the support channel.
2. A high-rate identical exception is usually infrastructure, not a bug. Check
   `/readyz` first:

   ```sh
   curl -s -w '\n%{http_code}\n' https://samaronefialho.dev/readyz
   ```

3. Any `sentry-sdk` upgrade re-runs the canary suite — see
   [email-delivery-failure](#email-delivery-failure) for the standing rule.

---

## Invitation revocation {#invitation-revocation}

**Detection signal** — a request through the support channel, or a decision made during
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

**Detection signal** — a request through the support channel (a leaver), or a security
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

1. Confirm the encarregado has been notified. `docs/lgpd.md` §3 defines the role, and
   [Roles and contacts](#roles-and-contacts) says who holds it. If the notification did
   not land, retry with `lgpd_renotify` — see
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

The commitment made to the firm is export on request, then erasure of the firm's data
within 30 days of the pilot's end, confirmed in writing
(`.evidence/PILOT2-000L-operator.txt`).

1. Confirm in writing what the firm wants done with its data, and record the answer.
2. Export anything the firm is entitled to take with it, before anything is removed.
3. Run [tenant-data-removal](#tenant-data-removal) for whatever the firm asks to have
   erased, subject to the retention regimes, and finish inside the 30 days.
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
| Cross-tenant or cross-client data exposure | User report, or an access-log review. **Immediate stop plus an LGPD art. 48 assessment**, and the 2-business-day clock in [lgpd-responsibilities](#lgpd-responsibilities) starts at detection. |
| Credential leak in any log or monitoring surface | Sentry event inspection after an `issue-alert-to-owner` mail, or the canary suite failing. |
| Document loss or checksum mismatch | `reconcile_documents` findings, or a user report. |
| Email outage over 24 hours | `issue-alert-to-owner` mails for the invitation or LGPD capture sites, plus the Resend delivery log and a test send. |
| Full outage over 4 hours | UptimeRobot M1a to M1d, M2 and M4 all DOWN for 4 hours, confirmed by `curl` to `/healthz` and `/readyz` from the operator's machine. |
| Unrecoverable backup failure over 48 hours | Healthchecks `app-mei-backup-lightsail` DOWN across two nightly runs, M1c DOWN on `backup: stale`, plus the `ops/backup.sh` exit code in `journalctl -t app-mei-backup`. |
| The pilot firm asks to stop | The support channel, or any other channel they use. |

**First response for any SEV-1** — pause invitations, notify the owner (SL) and the
security contact (SL) on the escalation channel, and write down the time. With one
person in both roles, the note with its time is what makes the notification real.
Assess before you remediate; a remediation that destroys the evidence makes the LGPD
assessment impossible.

**SEV-2 — degraded, users affected, not a stop condition.** Redis outage, storage
outage, stale scheduler. Acknowledge within 15 minutes during business hours (08:00 to
18:00 America/Sao_Paulo, Monday to Friday), best effort outside them, and tell the firm
what is degraded and what still works.

**SEV-3 — single-user or cosmetic.** Password reset, MFA recovery, a single failed
upload. Normal support flow.

Escalation is one-way during an incident: a SEV-3 that turns out to be a leak is a
SEV-1 from the moment you know, not from the moment it is convenient.

---

## Communications channels {#comms-channels}

Addresses live in the contact item (Proton Pass, vault `AWS/SaaS`, item
`app-mei pilot contacts`), never in this file.

- **Firm to support:** the support channel, the thread agreed with the firm at pilot
  start, watched 08:00 to 18:00 America/Sao_Paulo, Monday to Friday.
- **Support to firm:** the same channel, so the firm has one thread.
- **Escalation:** the owner's alert mailbox, used for SEV-1 and SEV-2. UptimeRobot,
  Healthchecks and the Sentry rule `issue-alert-to-owner` already mail it, so an alert
  and its escalation land in one place.
- **Statutory:** the `/lgpd/` channel and the encarregado mailbox
  (`LGPD_ENCARREGADO_EMAIL`), per `docs/lgpd.md`. Statutory requests are never handled
  informally on the support channel.

Rules that hold on every channel:

1. **Never** paste an invitation link, reset link, recovery code, token, or Sentry
   event body into any channel. Describe, do not quote.
2. During a SEV-1, state what is known, what is not, and when the next update comes.
   An update that promises nothing is still an update.
3. A monitor that is knowingly red gets a written explanation wherever the alert lands.
   A check that is red and unexplained is a check people learn to ignore.

---

## Business hours and response expectations {#business-hours}

- **Business hours:** 08:00 to 18:00 America/Sao_Paulo, Monday to Friday.
- **In hours:** the support contact (SL) acknowledges on the support channel within 15
  minutes.
- **Out of hours:** best effort. No acknowledgement time is promised.
- **On-call:** the owner (SL) is the only person on call, and alerts reach them through
  the owner's alert mailbox. There is no rota. Do not imply 24/7 coverage to the firm.

These values come from `.evidence/PILOT2-000L-operator.txt`. "Business days" in the
LGPD timeline below means these days.

Set these expectations with the firm in writing at pilot start. The commonest pilot
failure is not an outage; it is an outage the firm thought someone was watching.

---

## Rollback steps {#rollback-steps}

**Detection signal** — a bad release. UptimeRobot M3 goes DOWN when `/versionz` stops
matching its pinned SHA; Healthchecks `app-mei-verify-live` goes DOWN when the daily
workflow fails or never runs; or a SEV-1 is traced to a deploy. M3 also goes DOWN after
every legitimate deploy until its pin is updated, so read it against what you just did.

```sh
curl -s https://samaronefialho.dev/versionz
git ls-remote origin refs/heads/main
gh run list --workflow verify-live.yml -L 5
```

**First response** — decide which of two rollbacks you need. They are different
procedures with different risks:

- **Bad image** → roll back to `:previous`. Procedure in `ops/README.md` under
  rollback.
- **Bad data** → restore. Procedure in `ops/RESTORE.md`.

**Then:**

1. Confirm the target. Both anchors must exist on the host:

   ```sh
   docker image ls --filter reference="app-mei*" --format "{{.Repository}}:{{.Tag}} {{.ID}}"
   ```

   Expect `app-mei:previous` and `app-mei-caddy:previous`. The deeper anchor is the
   stopped OCI instance, kept until `v0.2.0-rc1` is tagged plus 7 days (`ops/README.md`,
   "Decommissioning the old host"); using it is an incident-approved procedure of its
   own, not a tag flip.
2. Execute the rollback per `ops/README.md`; it also documents the hard limit —
   image rollback cannot undo a migration, so a release that migrated is a restore,
   not a tag flip.
3. **Expect `/versionz` to report the older SHA, and expect that to make things red.**
   After a `:previous` rollback the box is intentionally running an older commit.
   `verify-live` compares `/versionz` against the head of `main` and turns red for as
   long as the rollback stands, and so do M3 and `app-mei-verify-live`; re-running the
   deploy job's assertion 5 by hand fails for the same reason. **That is the check
   working.** Do not "fix" it by editing the check or by re-deploying the bad image. If
   the rollback will stand for more than a short window, say so on the escalation
   channel. See [comms-channels](#comms-channels).
4. Let the red clear when the roll-forward lands.
5. Record the rollback, its reason, and its duration in the evidence set.

---

## Evidence retention {#evidence-retention}

- Pilot evidence lives in `.evidence/` as `PILOT-###-happy.txt`,
  `PILOT-###-failure.txt`, `PILOT-###-red.txt`, and `PILOT-###-operator.txt`, and the
  v2 execution's operator records as `PILOT2-###-operator.txt`. `.evidence/` is the
  execution worktree's local evidence root and is ignored by git.
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

- The **encarregado** role is defined in `docs/lgpd.md` §3 and reached at the
  encarregado mailbox, the value of `LGPD_ENCARREGADO_EMAIL`, published on `/lgpd/`.
  The owner designated the holder on 2026-09-21 (`.evidence/PILOT2-000L-operator.txt`);
  the name row in `docs/lgpd.md` §3 is still blank. The security contact (SL) isn't
  automatically the encarregado. If they are the same person, the contact item says so.
- The rights channel is `/lgpd/`, reachable without an account and exempt from the MFA
  gate on purpose.
- Encarregado notification is retried with `lgpd_renotify` — see
  [email-delivery-failure](#email-delivery-failure). Nothing retries it automatically.
- A cross-tenant or cross-client exposure triggers the LGPD art. 48 assessment
  described in `docs/lgpd.md` §4, in addition to the SEV-1 response. So does a
  credential leak or document loss that could have reached personal data.
- Erasure is governed by `docs/lgpd.md` §5 and `docs/retention.md`. Do not answer a
  subject directly about what can be deleted without reading them.
- No additional personal or behavioural collection is added for the pilot. The metrics
  plan in `ops/PILOT-METRICS.md` is built entirely on existing surfaces for this
  reason.
- Retention follows `docs/retention.md`: the access log is purged at 6 months, the
  business audit trail isn't purged in this phase, and fiscal artifacts carry at least
  5 years once they exist. Pilot exit doesn't shorten any of these.

**The incident timeline.** The clock starts at **detection**, not at confirmation.
`docs/lgpd.md` §4 sets 2 business days from detection for notifying the ANPD, and the
same window for affected subjects when the risk to them is relevant. Business days are
Monday to Friday, per [business-hours](#business-hours): an incident detected on a
Friday at 17:00 must reach the ANPD by the end of Tuesday.

| When | Step (`docs/lgpd.md` §4) | Who |
| --- | --- | --- |
| Detection | Write down the time. It is T0 for everything below. Open the SEV-1 and pause invitations | Whoever detects; the owner (SL) |
| Immediately | Contain: revoke the credential or connection involved, and record which tenants and tables a crossed boundary may have touched | Security contact (SL) |
| Immediately | Preserve: don't purge or rotate the access log, don't touch the audit trail, and snapshot `audit_accesslog` for the window | Security contact (SL) |
| Immediately | Notify the encarregado. The notification decision is theirs, not engineering's | Security contact (SL) to the encarregado |
| Before the deadline | Assess: categories of data, number of subjects, and whether the data was intelligible. This decides whether notification is required at all | Encarregado, with the security contact |
| By the end of business day 2 | Notify the ANPD through the *Comunicação de Incidente de Segurança* form at <https://www.gov.br/anpd/>: nature of the data, affected subjects, measures in place, risks, and mitigation taken | Encarregado |
| By the end of business day 2 | Notify affected subjects when the risk is relevant. For operator-role data (a firm's clients), notify the **firm**, which notifies its clients | Encarregado; the firm for its clients |
| After | Record the timeline, the decision, and the ANPD protocol number in the evidence set | Encarregado |

---

## Production access list {#production-access-list}

Rows name a holder by role and initials, never by email or phone. Every row must
still point at one real person; a row reading "the team" is not an access list. The
owner holds the operator, on-call, support and security roles, so one row covers them.

| Holder | Roles | Host SSH | Django admin (`is_staff`) | Object storage (OCI) | Sentry | Resend | UptimeRobot, Healthchecks | DNS (Cloudflare) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Owner, SL | Operator, on-call, support, security | Yes: key-based from one allow-listed IPv4 address, plus Lightsail browser SSH | No active account. The temporary administrator from the mailbox journeys is inactive | Yes, console | Yes, account owner | Yes, account owner | Yes, account owner | Yes |
| Encarregado | Encarregado (DPO) | No | No | No | No | No | No | No |

Sources: `.evidence/PILOT2-000-operator.txt` for the account owners and the SSH
firewall, `.evidence/PILOT2-106O-operator.txt` for the inactive administrator. The
encarregado role carries no production access. If its holder also holds the owner row,
that row's access applies and the contact item says so.

Rules:

1. Every `is_staff` account carries MFA. That is not a policy statement here — it is
   enforced, and `apps/accounts/mfa.py` puts `is_staff` in the required population as a
   union with firm membership specifically because a platform operator may hold no
   membership at all.
2. Credentials are never shared between people. A shared credential makes
   [emergency-access-recording](#emergency-access-recording) meaningless.
3. Secret locations, not secret contents, are documented. Production credentials, the
   Sentry DSN and the Healthchecks ping URLs are in Proton Pass, vault `AWS/SaaS`, item
   `Production recovery`; contacts are in item `app-mei pilot contacts` in the same
   vault. No secret value belongs in this repository.
4. Review this table at pilot start, at any personnel change, and at
   [pilot-exit-and-conversion](#pilot-exit-and-conversion).
5. Rotation triggers are listed in `ops/README.md` under secrets.

---

## Emergency access recording {#emergency-access-recording}

**Detection signal** — none. This entry exists because emergency access is the one
privileged action nothing else observes, which is exactly why it must be written down
by the person who took it.

Record, at the time and not afterwards, whenever any of these happens:

- Someone outside the [production access list](#production-access-list) is granted
  access.
- Someone inside it uses access they do not normally use.
- An `is_staff` account is activated. None is active today, so the first one is an
  emergency-access record.
- A credential is used out of hours.
- An operator resets a user's MFA authenticators (see
  [mfa-recovery](#mfa-recovery)).
- Anyone reads production data outside a named support request.

Each record carries:

1. Who — role and initials of one person, never a role alone.
2. When — start and end, with timezone.
3. What was accessed, and what was changed.
4. Why — the incident or request it belongs to.
5. How identity was verified, if the access was granted to someone out of band.
6. What was revoked afterwards, and when.

Records go into the evidence set per [evidence-retention](#evidence-retention) and are
reviewed by the security contact (SL). With one person in every role, that review is
self-review, so the record has to stand on its own for whoever reads it later. An
emergency access that was never recorded is indistinguishable, later, from an
intrusion.

---

## Related documents

- `ops/README.md` — the exercised mechanisms: roles, RLS, rate limits, credential log
  hygiene, backups, deploy, rollback, secrets, the email provider, object storage, host
  log rotation, host requirements, the two-phase bootstrap, and decommissioning. It also
  carries the operator runsheets with their as-executed records.
- `ops/RESTORE.md` — database and object recovery, ownership-pinned.
- `ops/PILOT-METRICS.md` — pilot metrics, their sources, and the stop conditions with
  their detection signals.
- `docs/lgpd.md` — LGPD controller/operator split, encarregado, incident runbook,
  erasure.
- `docs/retention.md` — retention regimes.
- `docs/residual-risks-pilot.md` — what is accepted and still open.

# Operations

Most of this file describes mechanisms that exist and have been exercised. The last
section, [Operator runsheets](#operator-runsheets-prepared-not-executed), is different:
it is procedures written ahead of execution for the pilot migration, and nothing in it
has been run. It is kept in the same file so the runsheets sit beside the mechanisms
they configure, and it is fenced off by that heading so the distinction cannot be lost.

## Database roles

Tenant isolation in this product is enforced by PostgreSQL row-level security, and RLS
is only real if the connecting role cannot step around it. Three roles exist for that
reason alone.

| Role | Flags | Owns tables | Used by |
| --- | --- | --- | --- |
| `app_migrator` | `LOGIN`, `BYPASSRLS` | yes | `manage.py migrate` only |
| `app_runtime` | `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS` | **no** | web, worker, beat |
| `app_test` | `LOGIN`, `CREATEDB`, `BYPASSRLS` | test database only | `pytest` |

`ops/sql/roles.sql` creates them. It is mounted into the `db` service at
`/docker-entrypoint-initdb.d/` and is idempotent, so `docker compose down -v && docker
compose up -d --wait` needs no manual step **to create the roles**. Its portal grants
are a different matter — see below.

For disaster recovery, do not rely on implicit init ownership. The restore runbook's
[`Roles recreation`](RESTORE.md#roles-recreation) section starts with `roles.sql`, creates
the target database under `app_migrator`, restores under that role, and reapplies grants.

Because entrypoint scripts only run against an **empty** data directory, editing
`roles.sql` requires `docker compose down -v` — a restart will not re-run it.

### The portal grants do not survive a new migration, so `migrate` re-issues them

`app_portal`'s two `GRANT` blocks are `to_regclass`-guarded, because the file runs under
`ON_ERROR_STOP=1` against a data directory with no tables in it. The guard means those
grants reach **only the tables that exist at the moment roles.sql is run** — which, on
the init hook, is none of them. `migrate` creates the tables afterwards.

So a table added by a migration gets its RESTRICTIVE policies from that migration and
**no grant at all**. The portal then meets `permission denied` on first use rather than
at deploy, and the test suite cannot see it: `tests/conftest.py` issues those grants to
the test database itself, so the tested database is correct while the deployed one is
not. This is exactly how the document vault reached staging with neither `SELECT` nor
`INSERT` on `obligations_document`.

#### The mechanism: `apps/core/grants.py`

`apply_portal_grants` is connected to `post_migrate` for the core `AppConfig`
(`dispatch_uid="core.apply_portal_grants"`), so it runs once at the end of every
`migrate` — which is exactly where the gap opens. It reads the allow-lists **out of
roles.sql** through the same parser `core.E012` uses, rather than a copy in Python, so
the artifact that declares the grants and the code that issues them cannot drift apart.

It computes the pairs the portal role is missing in ONE catalog query and grants only
those, which on a healthy database is none. And it never raises: another database
vendor, a cluster with no `app_portal`, a table no migration has created yet, and an
unreachable database all resolve to "do nothing" with a warning in the log.
`post_migrate` runs *inside* the `migrate` command, so an exception here would turn a
healing failure into a deployment that cannot migrate at all.

`flush` emits `post_migrate` too, so this receiver also fires on every
`TransactionTestCase` teardown — roughly 470 times per suite. Its roles.sql parse is
memoised and the clean path issues no DDL; keeping it near-free is a requirement, not a
nicety.

#### Every boot path checks exactly once, and only after healing

Django's management commands run their system checks **before** their handler, and
`migrate` inherits `requires_system_checks = "__all__"`. A drifted cluster would
therefore abort `migrate` on `core.E012` before `post_migrate` could heal anything —
the deadlock this design exists to break. So the commands that precede healing run
`--skip-checks`, and each path checks once afterwards:

| Boot path | Heals | Checks |
| --- | --- | --- |
| Development (`docker compose up -d --wait`) | `migrate --noinput --skip-checks` | `runserver`'s own check pass |
| Production (`docker-compose.prod.yml`) | `migrate --noinput --skip-checks` | the explicit `manage.py check` between `migrate` and `gunicorn` |
| CI container smoke | the stack's own entrypoint | `docker compose exec -T web python manage.py check`, post-boot |

`collectstatic` also carries `--skip-checks`, although it only ever evaluates the
`staticfiles` tag and could not have fired `core.E012`. The flag is there so the rule
reads as "nothing before the explicit check runs checks" rather than as a per-command
exception someone has to re-derive.

`call_command("migrate")` defaults to `skip_checks=True`, so test-database creation was
never affected by any of this. Recorded here so nobody "fixes" it.

Any deploy path added later must assert `manage.py check` after `migrate` for the same
reason the three above do: healing is silent, and the check is what makes a *failure* to
heal loud.

#### Fallback: re-running the grants by hand

Needed only when a cluster cannot be migrated — the healer covers the ordinary case.
Run the two `GRANT` blocks against the migrated database, as the table owner:

```sh
sed -n "/-- app_portal's allow-list./,\$p" ops/sql/roles.sql \
  | docker compose exec -T db psql "$DATABASE_MIGRATION_URL" -v ON_ERROR_STOP=1
```

#### `core.E012` is the backstop, not the fix

It refuses to boot when a table roles.sql declares exists without its grant. With the
healer in place it should never fire; if it does, something stopped the healer from
running — no `app_portal` role, a migration connection that does not own the tables, an
unreachable database — and `apply_portal_grants` will have logged a warning saying which.

`tests/conftest.py`'s own grant block is **kept** rather than deleted now that the healer
exists. It grants the test database directly, so a bug in the healer surfaces as a failed
assertion in `tests/core/test_portal_grant_healer.py` instead of turning every portal
test into a `permission denied` error that hides the assertion it was meant to make.

### Running migrations as `app_runtime` fails by design

`app_runtime` has no `CREATE` privilege on `public` and owns nothing, so `manage.py
migrate` under it aborts with `permission denied for schema public`. That is the
intended behaviour, not a misconfiguration. Migrations use `DATABASE_MIGRATION_URL`:

```sh
DATABASE_URL="$DATABASE_MIGRATION_URL" uv run python manage.py migrate
```

The `web` container already does this before starting the server.

### Why `app_migrator` has `BYPASSRLS`

Table owners are still subject to policies once `FORCE ROW LEVEL SECURITY` is set. A
`RunPython` data migration iterating a tenant-scoped table would otherwise see **zero
rows and report success**, producing a backfill that appears to work and does nothing.
`app_migrator` bypasses RLS; `app_runtime` never does.

### Why tests connect as `app_test`, not `app_runtime`

`pytest-django` must `CREATE DATABASE`, and transactional tests flush with `TRUNCATE`.
`app_runtime` can do neither. Tests therefore connect as `app_test` and issue `SET ROLE
app_runtime` per test (`tests/conftest.py`), which is what makes the isolation
assertions meaningful — PostgreSQL evaluates policies against the *current* role.

`app_test` is a member of both `app_runtime` and `app_migrator`. Without that
membership `SET ROLE` returns `permission denied to set role` and the entire isolation
suite fails on its first statement.

## Gunicorn runs the `gthread` worker, and UUIDv7 generation is locked

Primary keys are UUIDv7 values. `uuid6.uuid7()` advances a module-global
`_last_v7_timestamp` with a non-atomic read-modify-write and holds no lock of its own,
so under a threaded worker that update is unsynchronised.

**The exposure is monotonic ordering, not uniqueness.** A duplicate key would need the
48-bit millisecond field *and* the 76 bits from `secrets.randbits(76)` to agree.
Measured on this codebase at 16 threads × 4000 generations: zero duplicates and zero
repeated timestamps — which shows the race is rare, not that it is absent, and rarity
is not a guarantee.

The race is therefore removed rather than tolerated. Every runtime UUIDv7 default
routes through `apps.core.identifiers.uuid7`, which serialises the call on a
process-local lock. `UUIDv7PrimaryKeyModel` is the only runtime caller, and two tests
pin it: one asserts the lock is **held** during generation, the other asserts the field
default **is** the wrapper. Re-pointing it back at `uuid6.uuid7`, or deleting the
`with _LOCK`, turns the build red.

```sh
gunicorn config.wsgi:application --worker-class gthread --workers 2 --threads 2
```

> A no-duplicates assertion would be decoration here: unlocked generation produced none
> in 64000 attempts, so such a test passes with the lock removed. The tests assert the
> lock, not the outcome.

**UUIDv7 ordering is an index-locality property, never a business ordering guarantee.**
No caller may infer sequence, causality, or a timestamp from a primary key — sort on an
explicit column.

This is asserted in T-022. Celery's default prefork pool is process-based, so each
worker holds its own lock and needs no coordination; the wrapper is also what would
make a `threads` or `gevent` pool safe if one is ever adopted.

## Row-level security

### The `app.tenant_id` GUC differs between production and tests

`ALTER ROLE app_runtime SET app.tenant_id TO ''` applies at **login**. Production
connections therefore start with the empty string, while a test session that reaches
`app_runtime` through `SET ROLE` sees the setting **absent** (`NULL`). Both fail closed
through `NULLIF(current_setting('app.tenant_id', true), '')`, and the isolation suite
asserts both shapes explicitly.

## Client IP and the trusted-proxy count

Every per-IP control in this product — the login rate limit, the Marco Civil access
log, the IP recorded on a failed-login `PlatformEvent` — depends on knowing which
address a request actually came from. Getting that wrong fails silently in one of two
directions:

- **Trusting `X-Forwarded-For` when nothing rewrites it.** Any caller sets their own
  address, and every per-IP control becomes decorative.
- **Ignoring it behind a reverse proxy.** Every request reports the proxy's address,
  every per-IP control collapses into one shared bucket, and the first rate-limited
  user locks out everybody.

So it is configuration, not a guess. `TRUSTED_PROXY_COUNT` states how many proxies
this deployment actually controls:

| Deployment | `TRUSTED_PROXY_COUNT` | Effect |
| --- | --- | --- |
| Local development, container direct | `0` (default) | `X-Forwarded-For` is **not trusted**; `REMOTE_ADDR` is used |
| Single nginx/Caddy in front of gunicorn | `1` | The last entry appended by that proxy is used |
| CDN in front of a reverse proxy | `2` | The entry two from the right is used |

`apps.core.netaddr.client_ip` counts from the **right** of the header, because entries
to the left of the hops we control are attacker-supplied. Setting the count higher than
the real number of proxies makes the address spoofable; setting it lower collapses
everyone into the proxy's bucket. The default is `0`, which is the safe direction to
be wrong in.

## Rate limits

| Bucket | Default | Key | Why that key |
| --- | --- | --- | --- |
| Login / password reset | `5/m` | **email**, globally | Credentials are platform-global, so a tenant-keyed bucket would be a `Host`-header bypass worth 5 attempts × every firm |
| Login / password reset | `20/m` | **IP**, globally | Catches attempts spread across many addresses |
| Authenticated writes | `60/m` | `(tenant_id, user_id)` | The caller is identified and the tenant resolved from a membership they hold |
| Authenticated reads | `120/m` | `(tenant_id, user_id)` | `TenantMiddleware` holds a transaction open across rendering and the deployment holds only 4 concurrent request slots (`gthread`, 2 workers × 2 threads), so unbounded GETs are a denial-of-service vector |

All four are settings (`RATELIMIT_*`), so a deployment tightens them without a code
change. Buckets live in the Redis cache.

## Credential log hygiene

Credential-bearing URLs cross several independently configured boundaries. A clean
application audit row does not prove that the proxy, process server, error reporter or
host log driver is also clean. The current disposition of every known sink is:

> The two rows below marked **RESIDUAL RISK**, the operator gate on container log
> retention, and everything else the hardening work deliberately left open are written
> up in [`docs/residual-risks.md`](../docs/residual-risks.md). Read that page before
> assuming a boundary here is closed.

| Sink | Repository control | Status and boundary |
| --- | --- | --- |
| `audit_accesslog.path` | yes | **REDACTED.** Registered credential path segments are replaced before the 180-day access row is written. The visit remains recorded. |
| Django application logs | yes | **REDACTED AND MEASURED.** The production console handler replaces credentials resolved from the shared route registry before `django.request` formats handled 4xx or exception records. RESIDUAL-008 observed the redacted receipt and zero canary hits on success, refusal, rate-limit and safe-error paths. |
| Gunicorn access log | yes | **SANITIZED.** The explicit production format retains remote address, timestamp, method, status, bytes, duration and user-agent. It omits the request line, path, query string and Referer. No request/correlation header exists in the repository, so none was invented for this change. |
| Caddy access/error log | yes | **REDACTED AND MEASURED.** RESIDUAL-008 reproduced the former 502 leak as two URI-bearing error/access entries. Both the global error logger and the site access logger now delete `request.uri` and `request.headers.Referer` before serialization. Re-measurement found zero canary hits across success, refusal, rate-limit and safe-error paths; the 502 still produced two diagnostic receipts, each with no URI field. |
| Docker/container log retention | partial | **CONTENT SANITIZED; RETENTION OPERATOR GATE.** The aggregate local container trace had zero canary hits across all four measured paths after the Django and Caddy filters. Neither Compose file contains a `logging:` block, so driver choice and rotation remain host-level state that the operator must configure and verify. The prepared, not-yet-executed procedure is [Host log rotation](#host-log-rotation). |
| Sentry events and transactions | yes | **SANITIZED.** The SDK denylist is extended recursively for the live password and code fields. Both send hooks scrub request and Referer URLs, request data, transaction names, breadcrumb URLs, span URLs and repeated credentials in stack-frame locals. |
| CDN or load balancer | no deployed repository component | **NOT APPLICABLE.** Caddy is the only proxy represented in Git. Reclassify this row if another edge is introduced. |
| Browser history | no | **RESIDUAL RISK.** The credential remains in the address bar and history by design; the rejected token-exchange redesign is not reopened here. |
| Referer propagation | yes | **CLOSED GLOBALLY.** `SECURE_REFERRER_POLICY` is `strict-origin`, so same-scheme subrequests carry only the origin and HTTPS-to-HTTP downgrades carry no Referer. |
| Email scanners and link previews | no | **RESIDUAL RISK.** A mailed bearer link can be visited by recipient-side security tooling outside this repository's control. |

### Referrer-Policy supersession

The operator explicitly superseded RESIDUAL-005's instruction to keep the global
`SECURE_REFERRER_POLICY = "same-origin"` and stamp `origin` only on credential routes.
The approved policy is `strict-origin` globally in `config/settings/base.py`; production
inherits it. This strips paths on every route and also suppresses the Referer on an
HTTPS-to-HTTP downgrade. The global setting makes a per-route stamp in
`apps/security/csp.py` redundant, so no route-resolution machinery was added.

This remains compatible with Django's HTTPS CSRF enforcement. Requests carrying an
`Origin` are checked through that branch first. When `Origin` is absent, the strict
Referer fallback compares the HTTPS scheme and host, not the path; `strict-origin`
still supplies that origin on a same-scheme request. A policy that removed the Referer
entirely would break that fallback and is deliberately not used.

## Backups

`ops/backup.sh` takes a physical base backup, a logical dump, and prunes the WAL
archive. [`ops/RESTORE.md`](RESTORE.md) is the other half — how to get the data back.
Its recovery entry points are:

- [`Recovery timing status`](RESTORE.md#recovery-timing-status) — all PILOT-302/303
  results remain explicitly pending until the disposable rehearsals run.
- [`Secrets and environment`](RESTORE.md#secrets-and-environment) — password-manager
  source location and safe `.env.prod` reconstruction.
- [`Path A — physical restore with WAL replay`](RESTORE.md#path-a--physical-restore-with-wal-replay-rehearsed)
  — the local base-plus-WAL/PITR procedure and mixed-format archive checks.
- [`Path B — off-host logical restore`](RESTORE.md#path-b--off-host-logical-restore-ownership-pinned)
  — the ownership-pinned total-host-loss path.
- [`DNS`](RESTORE.md#dns) — Cloudflare inventory shape, TTL handling, and public checks.
- [`Object storage`](RESTORE.md#object-storage) — restored-row/live-byte Test A and
  `_probe/` version-recovery Test B.

The object-recovery layer those tests depend on is not armed yet. Bucket versioning and
the previous-version prune rule are an operator step with a prepared, not-yet-executed
procedure in [Object storage: versioning, lifecycle,
probe](#object-storage-versioning-lifecycle-probe).

### Off-host copies are provider-neutral and activated only after the provider gate

After every local phase succeeds, `backup.sh` writes the existing freshness marker and
completes the existing backup Healthchecks signal. Only then does it copy the physical
`base.tar.gz` and the logical dump with two explicit `aws s3 cp` commands to
`pg/<UTC-timestamp>/base.tar.gz` and `pg/<UTC-timestamp>/logical.dump`. A copy failure
exits nonzero and fails the dedicated off-host check, so systemd records it, while the
local marker remains stamped: that marker means **the local backup completed**, not that
the remote destination is healthy.

The destination is not tied to any provider and must not be assumed to be the expiring
OCI trial. The Wave-4 provider gate selects target-provider S3-compatible object storage,
or post-trial OCI only if PILOT-405 proves that its entitlement persists. Until that gate,
leave `OFFHOST_S3_BUCKET` absent; its presence is the script's activation switch. The
authoritative production copy evidence is deferred to PILOT-406. Local MinIO rehearsals
are only stand-ins and must never be described as production evidence.

The operator places these values in `/etc/app-mei/backup.env`; none belongs in git:

```text
HC_URL=<nightly-local-backup-check-url>
HC_OFFHOST_URL=<dedicated-offhost-copy-check-url>
OFFHOST_S3_ENDPOINT=<provider-s3-endpoint>
OFFHOST_S3_BUCKET=<dedicated-ops-bucket>
OFFHOST_S3_REGION=<provider-region>
OFFHOST_AWS_ACCESS_KEY_ID=<dedicated-put-only-access-key>
OFFHOST_AWS_SECRET_ACCESS_KEY=<dedicated-put-only-secret-key>
OFFHOST_AWS_SESSION_TOKEN=<optional-session-token>
OFFHOST_AWS_CONFIG_FILE=/etc/app-mei/offhost-aws.conf
```

The off-host identity is a separate least-purpose credential, never the document-bucket
credential. Grant object creation under `pg/` and no prefix-list permission. Put this
non-secret AWS CLI configuration at the path named above to force provider-neutral
path-style requests and SigV4:

```ini
[default]
s3 =
    addressing_style = path
    signature_version = s3v4
```

The host must provide AWS CLI v2. The script intentionally performs no remote listing or
deletion. In particular, it does not use a tree-mirroring operation: those operations
need prefix-list permission and are incompatible with the PutObject-only credential.
Verification is independent: a different read credential/session downloads each object
and compares byte count and SHA-256 with its local source.

`RETENTION_DAYS` still controls only local base backups, logical dumps, and their WAL
anchor. The off-host prefix is never pruned by this script; configure and record a
provider lifecycle at the Wave-4 gate, and do not make it shorter than the local recovery
window. The nightly off-host logical dump is the authoritative total-host-loss artifact.
Because this task does not continuously ship WAL, the copied base tarball is only a
best-effort extra and is not independently restorable after loss of the source host.

### The schedule is a host systemd timer, and it lives in this repository now

`ops/systemd/` holds `app-mei-backup.service`, `app-mei-backup.timer` and an idempotent
`install.sh`. Install or re-install with:

```sh
ssh app-mei 'sudo /opt/app-mei/ops/systemd/install.sh'
```

The installer refuses to proceed if `ExecStart` names a script that is not there, prints
a diff of anything it is about to overwrite, and runs `systemd-analyze verify` before it
enables anything — because a unit that is wrong only fails at 03:00, into nobody's inbox.

**That installer must not be used to bootstrap the migration target.** Its last action is
`systemctl enable --now app-mei-backup.timer`, unconditionally, which is correct on a host
already holding data and wrong on a host that holds none yet. The target copies the units
manually and leaves them disabled; see [New host
bootstrap](#new-host-bootstrap-two-phases).

**Those units existed on the box before they existed here.** They had been running since
2026-07-28 and were not in the repository, which is the same class of undocumented
external state as the box's git deploy key: a rebuilt host would silently have had no
backups at all and nothing would have said so. The files above are a faithful capture —
the schedule, the `User=ubuntu`, `Nice`, `IOSchedulingClass` and the jitter are the live
values, kept deliberately rather than re-chosen.

The timer is `03:00 America/Sao_Paulo` (06:00 UTC today) with up to ten minutes of
jitter, which is why the journal shows 03:02 and 03:07. It runs before beat's two sweeps
(03:30 and 04:00, also local, because they are scheduled under `CELERY_TIMEZONE`), and
`Persistent=true` means a box that was off at 03:00 runs on next boot rather than
skipping a night in silence.

It is a HOST unit, not a Celery beat task, and that is forced rather than stylistic:
`backup.sh` drives `docker compose exec` against the db container, so from inside a
container it would need the docker socket — root-equivalent here — bind-mounted into the
image that serves public HTTP, and it would make the backup depend on the very stack it
must be able to restore.

```sh
ssh app-mei 'systemctl list-timers app-mei-backup.timer --no-pager'
ssh app-mei 'journalctl -t app-mei-backup -n 50 --no-pager'
ssh app-mei 'sudo systemctl start app-mei-backup.service'   # force a run now
```

### A failed backup is visible at `/healthz`, and does not 503

The job's real failure mode is not a crash. It is a non-zero exit written into a terminal
that has already closed — which is exactly what happened between 2026-07-29 and
2026-07-31, when the unit sat in `failed` state for two nights and nothing said so.

So a successful local run stamps a freshness marker in
`obligations_schedulerheartbeat` (the table T-041's scheduler dead-man's switch already
uses, keyed by name), written only after every local phase succeeds. A failure before it
ages the marker out; a later off-host failure leaves this local fact intact and fails the
separate off-host signal plus the systemd unit instead. `/healthz` reports:

```json
{"status": "ok", "scheduler": "alive", "backup": "fresh", "disk": "ok"}
```

`backup` is `fresh`, `stale` (older than 36 hours, or never), or `unknown` (the database
could not be read). **A stale backup never changes `status` and never returns 503.** The
scheduler switch answers 503 because a dead beat means the application is not doing its
job; a stale backup means the application is fine and a person must act. Escalating it
would flap the container healthcheck every ten seconds, fail the deploy job's fourth
assertion, and take the site down over a problem the site does not have.

Thirty-six hours is one nightly cycle plus a half-day grace: long enough that a late or
slow run never alarms, short enough that one skipped night always does. The scheduler's
"three missed ticks" rule cannot be borrowed here — at one tick per day that is three
days of silence, and the outage this exists to catch lasted two.

### The backup tree's ownership is repaired on every run

`/backups` is the host directory `ops/backups` bind-mounted into the db container.
postgres inside it is uid 999, and the host keeps taking the tree back — a fresh checkout
creates the directory as the ssh user, `git reset --hard` in the deploy's Sync step
re-materialises any tracked path inside it the same way whenever the index's cached stat
data no longer matches, and if the directory is absent altogether Docker creates the
bind-mount source owned by **root**. When any of that happens the run dies at its first
`mkdir` with `Permission denied`.

`backup.sh` therefore re-asserts the invariant from inside the container as root,
recursively, immediately before its first write, and logs any path it had to repair. It
does not try to stop the host from breaking it; it takes it back every night regardless
of which host action did it.

**Nothing under `ops/backups/` may be tracked, and there is no `.gitkeep` there.** That
is the other half of the fix and it was bought the expensive way. Once the repair above
started chowning the directory to postgres, git on the box — running as the ssh user —
could no longer unlink the `.gitkeep` it had been keeping there, and deploy 30672150324
died at Sync with `error: unable to unlink old 'ops/backups/.gitkeep': Permission
denied`. Git and postgres cannot both own that directory. The backup has the stronger
claim, and Docker plus the db entrypoint keep the path alive without git's help.

### The WAL archive is bounded by retention, and the anchor is the OLDEST base

The prune runs `pg_archivecleanup` against the START WAL of the oldest **retained** base
backup, never the newest — deleting WAL newer than that would silently make every older
base backup unrestorable while appearing to succeed. A run that cannot read that label,
or reads one with no `START WAL LOCATION`, now exits 3 instead of warning: an unreadable
oldest base is not a pruning problem to defer, it means the earliest point this
deployment can recover to does not exist.

> **Open capacity decision, still not made.** The archive generates roughly 288 segments
> a day — ~4.5 GiB — because `archive_timeout=300` forces a switch every five minutes and
> each segment is a full 16 MiB whether or not it is full. At `RETENTION_DAYS=7` the
> steady state is ~34 GB of WAL beside ~14 GB of everything else, on a 45 GB disk: a
> perfectly working prune still fills it. Measured 2026-08-01: 15 GB of archive, 16.6 GB
> free, 64% used. The prune also cannot engage until the 2026-07-28 base ages out around
> 2026-08-04, so the trajectory until then is monotone.
>
> The three ways out all have real costs and none should be picked by whoever happens to
> be fixing a script:
>
> | Option | What it costs |
> | --- | --- |
> | Shorter `RETENTION_DAYS` | Narrows the recovery window — the oldest point you can restore to moves closer to now |
> | Longer `archive_timeout` | Loosens the documented ≤5 min RPO; a crash can lose up to the new interval |
> | `gzip` in `archive_command` | Changes `restore_command` too, so it changes the documented recovery procedure in `ops/RESTORE.md` |
>
> **What HAS been done is making the collision impossible to hit silently** — see the
> disk fuse below. The decision above is still owed.

### Resolved 2026-08-06: gzip was chosen, merged, deployed, and the archive compacted

**The decision above is made.** `wal-gzip` merged as `c921e3e` and is live. None of the
three costs in that table were paid: `RETENTION_DAYS` is still 7, `archive_timeout` is
still 300, the ≤5 min RPO stands, and every base backup and logical dump was retained.

It was forced by an incident rather than chosen at leisure. The disk reached 100% on
2026-08-04 and archiving wedged for 31 hours; the deploy that would have fixed it could
not run, because `git fetch` on the box needs free space. The way out was that
`archive_command` lives in the `db` service's compose `command:`, and `postgres:16` was
already on the box — so the compressed form could be activated by recreating **only**
`db`, with no image build, no pull, and no application deploy.

The existing 1,956 plain segments were then converted in place rather than pruned:
30.56 GiB → 90.36 MiB, an aggregate **346x**, disk free 623 MB → 30.85 GiB. Every
conversion was proven byte-identical before its original was unlinked, and the whole
archive was afterwards restored from end to end with matching content checksums —
`ops/RESTORE.md`, "Rehearsal 3" and "The one-time archive compaction of 2026-08-06".

Two things that incident established, which the sections below predate:

- **A partial `cp` poisons its own retry forever.** The old `archive_command` wrote
  straight to the final name, so an ENOSPC mid-copy left a truncated file there. The
  leading `test ! -f` then saw it and short-circuited on every subsequent attempt —
  archiving stayed wedged even after space was freed, because the blocker was the stub,
  not the disk. The shipped form writes `%f.part` and atomically renames, which is why
  it cannot happen again.
- **`pg_archivecleanup` is downstream of a guard the full disk trips.** It runs inside
  `backup.sh`, which aborts on `failed_count > 0`. Disk fills → archiving fails → the
  counter rises → the backup aborts → the prune that would free space never runs. The
  prune cannot be relied on to rescue a full disk; it is the first thing a full disk
  disables.

The original analysis is kept below because its measurements are what the decision rested
on, and because "what it would cost" is still the right frame if the question reopens.

What the measurement says, taken on the real archive: it is ~99.5% zero padding, because
`archive_timeout=300` forces a full 16 MiB switch every five minutes on a near-idle box
and the hourly histogram is flat at exactly 12 segments/hour. Aggregate `gzip` ratio
**219x** — 14.66 GiB of archive is ~68 MiB compressed.

| | Today | On the branch |
| --- | --- | --- |
| Per forced segment | 16 MiB | ~90 KiB (`gzip -6`, measured) |
| Burn | 4.57 GiB/day | ~26 MiB/day |
| Steady state at `RETENTION_DAYS=7` | ~32 GiB | ~180 MiB |
| Cost per segment | `cp`, 17 ms | `gzip -6`, 46 ms |

`-6` rather than `-1` (105x, 33 ms) or `-9` (198x, 83 ms): `-6` takes most of the ratio
for 29 ms more than a plain copy, and matches the `--compress=6` already used for
`pg_basebackup` and `pg_dump`. The archiver has 300 s between forced switches and spends
46 ms of it, a duty cycle of 0.015%; it would need to run roughly 6500x slower than
measured before it could fail to keep up at 12 segments/hour.

**Existing segments would be left exactly as they are.** Compressing them in place is a
separate, riskier operation — it rewrites the artifacts recovery depends on, while the
only thing standing behind them is the very archive being rewritten — and the branch does
not do it and does not need it to. The consequence is that the ~14.6 GiB already on disk
is **not** reclaimed by merging: it drains as `pg_archivecleanup` ages those segments out
over `RETENTION_DAYS`, so the reclaim arrives about a week after the flip and arrives on
its own. Until then the archive is **mixed**, and every part of the recovery path handles
both formats — that is what the rehearsal in `ops/RESTORE.md` exists to prove, and why a
`restore_command` that reads only `.gz` would be a regression rather than the new normal.

Two things the branch found that are worth knowing whichever way the decision goes:

- **`ALTER SYSTEM SET archive_command` is a silent no-op here.** The setting comes from
  the compose `command:` list, and argv outranks `postgresql.auto.conf`. The statement
  succeeds, writes the file, and changes nothing. Flipping the format means editing
  `docker-compose.prod.yml` and letting compose recreate the `db` container — a short
  outage, and never `down -v`.
- **`pg_stat_archiver.failed_count` cannot see a missing archiving binary.** Exit code
  127 is fatal to the archiver, which dies before it can report, so the counter stays 0
  while nothing is archived. `ops/backup.sh` therefore now also refuses to run when
  segments are queueing in `pg_ls_archive_statusdir()`. That gap exists on `main` today —
  it is just harder to reach with `cp`, which is always present, than with `gzip`.

### The disk fuse: `/healthz` reports headroom, and does not 503

The capacity problem above had no signal at all. Free space was visible only to somebody
who ssh'd in and ran `df`, which means the first symptom of the collision would have been
a database that could no longer write. So the scheduler measures the disk on every tick
and `/healthz` reports it:

| `disk` | Meaning |
| --- | --- |
| `ok` | The scheduler's last tick measured at least 12 GiB free |
| `low` | It measured less than that — **act; this is not self-healing** |
| `unknown` | Nothing has been measured, or what was measured is too old to trust |

**`low` never changes `status` and never returns 503**, for exactly the reason a stale
backup does not: a full-ish disk needs an operator, it does not mean the application is
broken. Escalating it would flap the container healthcheck every ten seconds, fail the
deploy job's fourth assertion, and take the site down over a capacity warning.

#### The number is minutes old, and it used to be a night old

The reading rides the beat heartbeat (`record_heartbeat`, every five minutes), not the
nightly backup. It was the other way round for exactly one commit, and the arithmetic of
that version is why it changed: against the projected floor crossing of
**2026-08-01T21:41Z**, a marker stamped by the 03:00 job did not read `low` until
**2026-08-02T06:07Z** — the fuse blowing **8.5 hours after the fault**. A capacity fuse
whose latency is most of a night is close to no fuse at all on the last night.

The premise that forced the nightly design was that only a host script can see the host
filesystem. **That premise is false.** A container's `/` is an overlay whose upper layer
lives in the daemon's snapshot store, and `statvfs` on an overlay is answered by the
filesystem backing that layer — so a container measuring its own root reports free space
on the filesystem where image layers and volumes are really written, which is exactly
what the deploy job's `MIN_FREE_KIB` preflight is asking about. Verified against the box
2026-08-01T03:27Z: the worker container answered **16,230,788 KiB** available, the host's
`df -Pk /` answered **16,230,784 KiB**, four KiB apart.

This is deliberately *not* `docker info --format '{{.DockerRootDir}}'` plus `df`, which
is what `backup.sh` used to do — and which was a no-op twice over here. `/var/lib/docker`
is not a separate mount on this box (it is `/` on `/dev/sda1`), and the daemon runs the
**containerd snapshotter**, so the layers that lookup was aiming at actually live under
`/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/`. Measuring the container's
own writable layer requires neither fact to hold.

`ops/backup.sh` therefore no longer stamps free space, and **there is exactly one writer**
of `free_disk_kib`, on the `beat` row. Keeping the nightly stamp as a secondary source
would have produced two readings that disagree every time the prune actually freed space —
an hours-old `low` against a minutes-old `ok` — with nothing in the probe able to say
which one described the present. `tests/obligations/test_disk_headroom.py` reads
`ops/backup.sh` and fails if the column reappears in its executable body.

Three properties are deliberate and each has a test:

- **A stalled heartbeat downgrades `ok` to `unknown`.** Carried over unchanged from the
  nightly design, because the reasoning survived the move: a dead writer means nobody is
  watching the number, and a dead scheduler is *also* a WAL prune that is not being
  triggered — so the disk fills fastest in exactly the window where the reading stops
  being refreshed. Reporting the last comfortable value there would be the fuse lying at
  the only moment it is load-bearing.
- **A stalled heartbeat keeps `low`.** Nothing frees space unless the prune runs, so an
  old low reading is at worst an understatement, and discarding it would throw away a
  true alarm.
- **The probe never measures.** `/healthz` answers from the stored number and issues no
  syscall. It is hit every ten seconds by the container healthcheck against four
  concurrent request slots, and the value only changes on beat's cadence anyway.

The measurement is `statvfs`, not a `df` subprocess — that removes failure modes rather
than handling them: no fork on a box short of memory, no timeout to arm, no stdout to
misparse. Its entire failure surface is `OSError`, which is caught, logged, and answers
`None`; `/healthz` renders that as `unknown`. **A failed measurement must never abort the
heartbeat**, because an exception escaping there would leave the liveness stamp unwritten,
flip the probe to 503 fifteen minutes later, and turn a capacity *warning* into an outage.

#### Why 12 GiB, and why a fixed floor rather than a percentage

The floor is derived, not chosen. The deploy job refuses to land an image pair with less
than 3 GiB free (`MIN_FREE_KIB` in `.github/workflows/ci.yml`), and the archive accrues
~4.5 GiB a night. `3 + 2 × 4.5 = 12 GiB` is therefore the smallest floor that fires
**while deploys still work** — a warning that only arrives after deploys have started
failing is not a warning — and still leaves **two nights** to act.
`tests/obligations/test_disk_headroom.py` reads `MIN_FREE_KIB` out of the workflow and
recomputes `3 + 2 × 4.5` against it, so the two floors cannot drift apart silently and a
second hardcoded copy of the floor could not agree with the derivation.

**Making the reading fresh did not buy back any of the floor**, and the runway is still
counted in nights. Alert latency and response time are different quantities: what the
fuse demands is a capacity decision — shorten `RETENTION_DAYS`, lengthen
`archive_timeout`, or gzip `archive_command`, each trading recovery window, RPO or
restore procedure — and that is days of an operator's attention no matter how promptly
the alarm lands. What the five-minute cadence bought is that the alarm lands *when the
floor is crossed* rather than the next morning.

It is an absolute byte count rather than a share of the disk because WAL accrues at a
fixed 16 MiB per forced switch regardless of how large the disk is. The question an
operator needs answered — *how many nights do I have* — is `free / rate`, an absolute. A
percentage floor would silently shorten the lead time on a smaller disk and lengthen it
on a larger one, which is backwards.

## Deploy

Staging deploys itself. A push that lands on `main` and passes both gates ships to the
box with no human in the loop, and the job proves the deployment landed by observing
its **effects** rather than by trusting that the containers came up. Everything below
describes `deploy-staging` in `.github/workflows/ci.yml`; the manual path exists for
the day GitHub is unavailable, not as the normal route.

### The job

The trigger is `push` on `refs/heads/main`, and both halves of that condition are
load-bearing. `push` excludes `pull_request` runs, whose head is a synthetic merge
commit that exists on no branch and therefore cannot be checked out on the box; the ref
check excludes any branch a future trigger might add. A pull request still runs `test`
and `container-smoke` — it just never reaches the deploy.

Gating is `needs: [test, container-smoke]`, so lint, types, the migration check, the
full suite, the isolation suite and a cold-booted `/healthz` all pass before anything
touches the server.

The mechanism is deliberately unglamorous:

1. **Build both images on the runner** with plain `docker build`, never `docker compose
   -f docker-compose.prod.yml build`. That file carries roughly fifteen mandatory
   `${VAR:?}` interpolations and no `.env.prod` exists on a runner, so compose aborts
   while *parsing*, before it would ever reach a build. The app image takes
   `--build-arg GIT_SHA`; the Caddy image is built from `ops/caddy`.
2. **Sync the checkout** with `git fetch --prune origin && git reset --hard "$GIT_SHA"`.
   The box needs the tree as well as the images, because compose reads
   `docker-compose.prod.yml`, `ops/Caddyfile` and `ops/sql` from disk there. **This runs
   before anything lands on the box**, and the ordering is load-bearing — see below.
3. **Anchor the rollback** by tagging the currently running images `:previous` on the
   box — before the load, because once `docker load` overwrites the `:prod` tags the
   previous generation is unreachable by name.
4. **Ship the pair** as `docker save … | gzip -1 | ssh 'gunzip | docker load'`. The
   images are never built on the server; see the next section for why.
5. **Roll the stack** with `up -d --no-build --wait`, no `--force-recreate` and no
   service list. Compose already recreates exactly the containers whose image id moved,
   and naming services would silently skip any service added to the file later.
 6. **Assert five times, by effect** (below), then run the post-rollout storage probe,
    and only then `docker image prune -f`. Pruning earlier would delete the layers the
    `:previous` tags depend on, destroying the rollback while the deploy was still
    unproven.

#### Sync before the load, because only one of those two is reversible

Steps 2 and 4 do not depend on each other. The Sync touches only git under
`$DEPLOY_PATH`; the load touches only the docker daemon. Neither reads what the other
writes, so the order is free to choose — and exactly one choice is safe.

`docker load` **overwrites** the `:prod` tags. While Sync ran after the load, a Sync
failure left `app-mei:prod` naming an image the stack was not running, on a box whose
checkout was still on the previous commit. That is not hypothetical: deploy
`30672150324` shipped both images successfully and then died at Sync on `error: unable
to unlink old 'ops/backups/.gitkeep': Permission denied`, leaving precisely that split.
The stack kept serving the old containers, so nothing was down — but `:prod` was a lie,
and the next `compose up` anyone ran by hand would have rolled a new image under an old
tree.

Syncing first inverts that. The cheap, fully reversible half fails before the
irreversible half has happened at all, and a failed deploy leaves the box in the state
it started in. The disk preflight moved with it and now runs *after* the Sync, so the
free space it measures is what the load will actually find — the checkout writes to the
same filesystem as the docker data root.

The five assertions are the point of the job:

| # | Assertion | What it catches |
| --- | --- | --- |
| 1 | `/app/RELEASE` inside the running `web` container equals the pushed SHA | A stack that came back up on the **old** image |
| 2 | `showmigrations --plan` exits 0, shows at least one `[X]`, and shows no `[ ]` | Migrations that never ran, and a probe that died instead of reporting |
| 3 | `manage.py check` inside the deployed container | Silent failure of the portal-grant healer, and every other system check |
| 4 | `GET https://samaronefialho.dev/healthz` returns 200 with `"status": "ok"`, resolved onto the deploy target's own address | Caddy, TLS and DNS, which nothing inside the stack can see |
| 5 | `GET https://samaronefialho.dev/versionz`, resolved onto the deploy target, reports `.release == $GIT_SHA` | An edge that answers from some *other* box, or from a stack whose public face is not the commit just shipped |

Assertions 1 and 5 look like the same question and are not. Assertion 1 reads
`/app/RELEASE` through ssh, from inside the container — it proves the box took the
image. Assertion 5 reads `/versionz` from the runner through the public TLS edge, so it
proves the thing the internet reaches serves that commit. A stack can pass 1 and fail 5.

Both 4 and 5 pin resolution with `curl --resolve samaronefialho.dev:443:$DEPLOY_IP`,
where `DEPLOY_IP` comes from `getent ahostsv4 "$DEPLOY_HOST"`. That is what makes them
assertions about **the box just deployed** rather than about whatever DNS currently
points at — which matters most during a host migration, when the two are different
machines on purpose.

The **step names in `.github/workflows/ci.yml` still read `1/4` through `4/4`, then
`5/5`.** The labels were not renumbered when the fifth was added; only the last one is
self-consistent. Count the steps, not the labels.

Assertion 2's positive control is not decoration. A bare `! … | grep -q '\[ \]'` passes
when the command inside it dies, because a dead container emits nothing and nothing
contains no pending marker. So the plan is captured to a file, the probe's own exit
status is asserted, at least one applied migration is required, and only then is
"none pending" meaningful. The first real deploy reported `assert 2/4 ok: 94 migrations
applied, none pending`.

#### After the assertions: the storage probe is verification, not a gate

The job's last check before the prune is
`docker compose exec -T web python manage.py storage_probe`, which writes, reads back,
refuses anonymously, and deletes a `_probe/` object against the configured bucket. It is
deliberately placed **after** the rollout rather than before it.

That ordering is the whole point. The stack is already serving by the time the probe
runs, so a probe failure marks the deploy red without stranding traffic between two
stacks — which is what a pre-rollout storage gate would do the first time the object
store had a bad minute. The signal is "documents are broken, act now", not "hold the
release". Storage is also the one dependency `/healthz` says nothing about: the probe is
the only place in the deploy where document bytes are exercised end to end.

The probe has so far run only against local storage. Running it against the production
bucket, and arming versioning and the lifecycle prune behind it, is [Object storage:
versioning, lifecycle, probe](#object-storage-versioning-lifecycle-probe).

### Why container health is not deployment proof

Both of the rules above were bought the expensive way, during the manual T-065 deploy.
They are quoted here in full rather than referenced, because the QA artifacts that
recorded them are regenerated per run and are not in the repository.

On building Caddy on the server:

> Building Caddy on the box is NOT viable: xcaddy compiles the full Caddy dependency
> tree and ran 30+ minutes on 1 vCPU while swap-thrashing against live Celery, without
> finishing. Both images were built locally and shipped with `docker save | ssh docker
> load`. That should be the documented deploy path.

On trusting a green `up -d`:

> `--no-build` is a trap here. `web` is also built from source, so the first attempt
> restarted it on the OLD image: containers went healthy, manage.py check passed
> (trivially — that image has no E008), and NO migrations ran. Caught only by querying
> the database. Verify migrations by their effect, never by container health.

Every container was healthy, the check was green, and the deployment had not happened.
That is why `--no-build` in step 5 is safe *only* because steps 1 to 3 guarantee a new
image is already loaded, and why assertions 1 and 2 exist at all.

### Rollback

There are two failure classes and they have different answers. Diagnose which one you
have **before** touching anything, because the wrong choice makes the second attempt
harder.

#### Bad image: roll back to `:previous`

Both images are anchored, so both roll back. Retag `:previous` over `:prod` and bring
the stack up without building:

```sh
ssh app-mei '
  set -eu
  docker tag app-mei:previous app-mei:prod
  docker tag app-mei-caddy:previous app-mei-caddy:prod
  cd /opt/app-mei
  docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --no-build --wait
'
```

If the bad release also changed `docker-compose.prod.yml`, `ops/Caddyfile` or `ops/sql`,
reset the checkout to the previous commit as well (`git -C /opt/app-mei reset --hard
<previous-sha>`) before the `up`, or compose will roll the old image under the new
file.

Two notes on the anchors. The job writes both `:previous` tags **before** each load, so
they always point at the generation that was serving traffic a moment ago. And
`app-mei-caddy:previous` did not exist after the first CI deploy: `app-mei-caddy:prod`
was not on the box yet, so `docker tag app-mei-caddy:prod app-mei-caddy:previous`
printed `No such image` and the job's `|| true` absorbed it, exactly as designed. From
the second deploy onward both anchors are real. Check before relying on one:

```sh
ssh app-mei 'docker image ls --filter reference="app-mei*" --format "{{.Repository}}:{{.Tag}} {{.ID}}"'
```

##### A deliberate rollback makes `/versionz` and `verify-live` go red, correctly

After a `:previous` rollback the box is intentionally running an older commit, so
`/versionz` reports that **older** SHA. Everything downstream of it then disagrees with
`main`, and all of it is right to:

- The scheduled `verify-live` workflow compares `/versionz` against the head of `main`,
  so it turns **red for as long as the rollback stands**. That is the check working: the
  deployed release genuinely is not the released one.
- Re-running the deploy job's assertion 5 by hand against `$sha` of `main` fails for the
  same reason.

Do not "fix" either by editing the check or by re-deploying the bad image. Treat the red
as the open-incident indicator it is, and let it clear when the roll-forward lands. If
the rollback is going to stand for more than a short window, say so wherever the alert
is received — a check that is knowingly red and unexplained is a check people learn to
ignore.

#### Bad data: restore

If the release corrupted or deleted data, the image is not the problem and retagging
will not help. Follow [`ops/RESTORE.md`](RESTORE.md), which covers both the physical
restore with WAL replay and the logical path, and ends in a verification block that
must not be skipped.

#### The schema caveat: image rollback has a hard limit

**Rolling back the image is valid only while the migrations applied in between are
backward-compatible.** The previous image's ORM talks to the *current* schema, and
Django does not negotiate.

`0016_remove_obligationtype_due_rule` is the live example. It drops
`obligations_obligationtype.due_rule`, which the previous image's ORM still names in
every `SELECT` it builds for that model, so after that deploy an image rollback leaves
every `ObligationType` query raising `UndefinedColumn`. There is no forward fix from
that state either: `0016` is documented one-way in its own module docstring, because
its reverse re-adds a `NOT NULL` column with no default onto a populated table and
PostgreSQL rejects it outright.

So: **rollback across a schema-narrowing deploy is the RESTORE path, not the image
path.** When a release contains a migration that drops or renames anything, say so in
the commit message, and treat `ops/RESTORE.md` as the only rollback that exists for it.

### Secrets, and what they are worth to an attacker

Five repository secrets drive the job: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`,
`DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS`. They arrive as environment variables rather
than `${{ }}` interpolated into the shell script, because interpolation splices the
value into the shell *source*, where a newline or a quote in a secret becomes
executable text. The private key is written to `$RUNNER_TEMP`, used through a wrapper
that sets `IdentitiesOnly=yes` and `IdentityAgent=none`, and shredded in an
`if: always()` step.

The blast radius is worth stating without euphemism. The deploy key authenticates as
`ubuntu`, and that account's groups include `docker`. Membership in `docker` is
**root-equivalent**: it permits starting an arbitrary container with an arbitrary host
bind-mount, which is a complete filesystem read/write as root by design of the daemon,
not by a bug. Therefore anyone who can push to `main`, and anyone who extracts
`DEPLOY_SSH_KEY`, controls the VPS. That includes the Postgres data volume and
`/opt/app-mei/.env.prod`, which holds every production credential, including
`OCI_S3_SECRET_ACCESS_KEY` and therefore the offsite backups.

This is an accepted trade, approved at the planning gate: a solo staging box on a
single-maintainer repository does not carry the operational weight of a hardened deploy
account with a rootless daemon and a restricted `command=` in `authorized_keys`. It is
a deliberate decision with a known cost, recorded here so that the decision is visible
when the cost changes — a second contributor, real customer data, or a move off
staging all change it.

#### Rotate on any of these triggers

- **A contributor is added.** They gain push access to `main`, which is box control.
- **A contributor is removed.** Their access to the box outlives their access to the
  repository otherwise.
- **A suspected leak** of the key, a runner compromise, or a workflow that logged more
  than it should have.
- **Routine regeneration**, so that the procedure is known to work before it is needed.

The procedure is three steps and the middle one is easy to leave half-done:

```sh
# 1. New keypair, no passphrase (the runner cannot answer a prompt).
ssh-keygen -t ed25519 -f /tmp/deploy_new -C 'gha-deploy' -N ''

# 2. Replace the secret. --body or stdin is mandatory: `gh secret set` with neither
#    blocks forever on an interactive prompt inside a non-interactive shell.
gh secret set DEPLOY_SSH_KEY -R samaronejr/app-mei < /tmp/deploy_new

# 3. Authorise the new key, then REMOVE the old line. Adding without removing leaves
#    the rotated-out key valid, which is not a rotation.
ssh-copy-id -i /tmp/deploy_new.pub app-mei
ssh app-mei 'vi ~/.ssh/authorized_keys'   # delete the previous gha-deploy entry
```

Verify the new key in isolation before deleting anything local: `ssh -i /tmp/deploy_new
-o IdentitiesOnly=yes -o IdentityAgent=none ubuntu@<host> 'true'`. Without both options
ssh falls back to an agent identity or `~/.ssh/id_*`, so a green connection proves only
that *some* key works.

### The box's git remote is undocumented external state

The Sync step depends on `git fetch origin` succeeding **on the box**, which works today
because a GitHub deploy key was added to the server in an earlier session. This project
did not create that credential, cannot see it, and does not manage it. If it is ever
revoked, the deploy fails at Sync with an authentication error while every earlier step
looks fine.

The recovery path does not need the remote at all, and was the actual mechanism of the
T-065 deploy, so it is known to work. Push the history over the same ssh channel as a
bundle:

```sh
git bundle create /tmp/app-mei.bundle main
ssh app-mei 'cat > /tmp/app-mei.bundle' < /tmp/app-mei.bundle
ssh app-mei "
  set -eu
  cd /opt/app-mei
  git fetch /tmp/app-mei.bundle main
  git reset --hard FETCH_HEAD
  git rev-parse HEAD
"
```

A permanent fix is to add a fresh read-only deploy key for the repository to the box's
`~/.ssh/`, at which point the Sync step works again unchanged.

### A deploy can interrupt beat, and that is not a bug

Two scheduled tasks run in the early morning (`config/settings/base.py:348-368`):
`purge-access-logs` at 03:30 and `refresh-das-calendars` at 04:00. A deploy recreates
the `worker` and `beat` containers, so a deploy landing in that window can kill a task
mid-flight.

Nothing needs to be done about it. `CELERY_TASK_ACKS_LATE = True`, so a task whose
worker dies is redelivered rather than lost, and both of these tasks are idempotent:
the purge is a bounded delete over a date threshold, and the calendar refresh writes
through a uniqueness constraint that makes a repeat a no-op. The stack self-heals on
the next tick. This is written down so that nobody spends 4am debugging a non-bug.

### Concurrency: a `cancelled` deploy is often the correct outcome

The job declares `concurrency: { group: deploy-staging, cancel-in-progress: false }`.
Never cancelling in flight is the important half: a half-loaded image or a stack caught
mid-`up` is a worse state than a queue.

The consequence is that GitHub retains only the **newest pending** run per group, so
back-to-back pushes supersede one another and the superseded deploy reports `cancelled`
without ever running. That is by design and not a failure. Anything that verifies deploy
history must therefore read the latest **completed** run, not the latest run:

```sh
gh run list -R samaronejr/app-mei --workflow=ci.yml --branch=main --status=completed --limit=1
```

### Manual fallback

For when GitHub Actions is unavailable. This is the CI job by hand, in the same order,
and it must be run from a clean checkout of the commit being deployed.

```sh
sha="$(git rev-parse HEAD)"

# 1. Build both images. The --build-arg is not optional: without it /app/RELEASE bakes
#    the Dockerfile default `unknown`, and the NEXT CI deploy's assertion 1 fails --
#    correctly, because the box would not be running what it claims.
docker build --build-arg GIT_SHA="$sha" -t app-mei:prod .
docker build -t app-mei-caddy:prod ops/caddy

# 2. Sync the checkout (or use the bundle fallback above) BEFORE any image lands. This
#    is the reversible half; once the load overwrites :prod it cannot be undone by
#    re-running, and a failure here would strand :prod against a stale tree.
ssh app-mei "cd /opt/app-mei && git fetch --prune origin && git reset --hard $sha"

# 3. Anchor the rollback BEFORE the load, or the previous generation loses its name.
ssh app-mei '
  docker tag app-mei:prod app-mei:previous || true
  docker tag app-mei-caddy:prod app-mei-caddy:previous || true
'

# 4. Ship the pair.
docker save app-mei:prod app-mei-caddy:prod \
  | gzip -1 \
  | ssh app-mei 'gunzip | docker load'

# 5. Roll the stack.
ssh app-mei 'cd /opt/app-mei && docker compose --env-file .env.prod \
  -f docker-compose.prod.yml up -d --no-build --wait'
```

Then run all **five** assertions by hand, and the storage probe after them. A manual
deploy that skips them is exactly the T-065 situation described above — and one that
runs only the first four proves nothing about what the public edge actually serves.

`deploy_ip` is the same pin CI uses. Resolve the deploy target's own address once and
send both public assertions to it explicitly, or a stale DNS record will let some other
box answer for the name and both checks will pass against the wrong machine.

```sh
compose='docker compose --env-file .env.prod -f docker-compose.prod.yml'
deploy_ip="$(getent ahostsv4 <deploy-host> | awk 'NR==1{print $1}')"

# 1/5 — the running container is this commit.
ssh app-mei "cd /opt/app-mei && $compose exec -T web cat /app/RELEASE"          # == $sha

# 2/5 — migrations applied, with the positive control.
ssh app-mei "cd /opt/app-mei && $compose exec -T web sh -c \
  'DATABASE_URL=\"\$DATABASE_MIGRATION_URL\" python manage.py showmigrations --plan --skip-checks'" \
  | grep -c '\[X\]'                                                             # >= 1, and no [ ]

# 3/5 — system checks green in the deployed container.
ssh app-mei "cd /opt/app-mei && $compose exec -T web python manage.py check"

# 4/5 — /healthz is ok over the public edge, pinned to the deploy target.
curl -fsS --max-time 30 --resolve "samaronefialho.dev:443:$deploy_ip" \
  "https://samaronefialho.dev/healthz?cb=$sha" \
  | jq -e '.status == "ok"'

# 5/5 — the deploy target's public face runs THIS commit.
curl -fsS --resolve "samaronefialho.dev:443:$deploy_ip" \
  "https://samaronefialho.dev/versionz?cb=$sha" \
  | jq -e --arg GIT_SHA "$sha" '.release == $GIT_SHA'
```

`jq -e` is what makes assertions 4 and 5 bite: it exits nonzero when the predicate is
false, so a 200 carrying the wrong body fails instead of scrolling past. Retry 4/5 a
couple of times before believing a refusal — a just-recreated `web` container can still
be draining its first requests, which is why the CI step loops three times with a
10-second pause.

Finally, the post-rollout storage probe. It is verification, not a gate: the stack is
already serving, so a failure here means documents are broken and needs acting on
immediately — it does not mean the release should be held back.

```sh
ssh app-mei "cd /opt/app-mei && $compose exec -T web python manage.py storage_probe"
```

### Command-to-evidence crossref

Every command above has actually been run. The right-hand column names where, so that
a reader can tell documented-and-exercised from documented-and-plausible. QA artifacts
are regenerated per run and are not tracked in the repository, so they are named rather
than linked.

| Command | Exercised by |
| --- | --- |
| `docker build --build-arg GIT_SHA=… -t app-mei:prod .` | todo 5 QA, `FIX-05-happy`; every CI deploy |
| `docker build -t app-mei-caddy:prod ops/caddy` | todo 5 QA, `FIX-05-happy` |
| `docker tag app-mei:prod app-mei:previous` | CI run 30643246807, Anchor step |
| `docker tag app-mei-caddy:prod app-mei-caddy:previous` | CI run 30643246807 (no-op on the first deploy, as designed) |
| `docker save … \| gzip \| ssh 'gunzip \| docker load'` | T-065 manual deploy; every CI deploy |
| `git fetch --prune origin` / `reset --hard <sha>` | todo 4 QA, `FIX-04-happy`; CI run 30643246807, Sync step |
| `git bundle create` + `git fetch <bundle>` | T-065 manual deploy (21 commits shipped this way) |
| `docker compose … up -d --no-build --wait` | T-065 manual deploy; CI run 30643246807 |
| `manage.py showmigrations --plan --skip-checks` | CI run 30643246807, assert 2/4 (94 applied, none pending) |
| `manage.py check` | todo 1 QA, `FIX-01-happy`; CI run 30643246807, assert 3/4 |
| `curl … /healthz` | todo 3 QA, `FIX-03-happy`; CI run 30643246807, assert 4/4 |
| `curl --resolve … /versionz` + `jq -e '.release == $GIT_SHA'` | PILOT-202 QA (endpoint and assertion shape). **Not yet observed against a live deploy** — the first CI run carrying it has not landed, and PILOT-002 owns the live observation |
| `manage.py storage_probe` | PILOT-203 QA (`PILOT-203-happy`, against local storage). **Not yet run against the real bucket** — PILOT-204 owns that |
| `docker image ls --filter reference="app-mei*"` | todo 6 QA, `FIX-06-happy` (live, against the box) |
| `docker image prune -f` | CI run 30643246807, Reclaim step |
| `gh secret set` / `gh secret list` | todo 4 QA, `FIX-04-happy`; todo 6 QA, `FIX-06-happy` |
| `gh run list --status=completed` | todo 6 QA, `FIX-06-happy` |

### One orphan image on the box

The box still carries `app-mei-caddy:latest` from before the images were renamed to the
`:prod` tags the compose file now references. Nothing points at it and nothing will; it
costs 154 MB and `docker image prune -f` will not touch it because it is tagged. Untag
it (`docker rmi app-mei-caddy:latest`) or leave it. Recorded so that its presence is not
mistaken for a live tag during a rollback.

## Operator runsheets: prepared, NOT executed

Everything under this heading is a **procedure to execute**, not a record of execution.
No step below has been run, no account below exists yet, and no value below has been
observed. The sections were written by the executor so the operator has exact commands
and exact acceptance evidence to work from; the as-executed transcripts land in the QA
evidence artifacts named in each section, and only then does anything here become a
statement about reality.

Two conventions make that unambiguous, and they are load-bearing:

- Any value only the operator can supply is written as an angle-bracket
  `<placeholder>`: regions, bucket names, IAM ARNs, instance IPs, DNS record values,
  quotas, expiry dates, and every measured timing. A placeholder that survives into a
  transcript is an unfinished step, not a formatting artifact.
- Any result not yet observed is written `PENDING`. There are no plausible-looking
  sample outputs anywhere in this block, because a plausible sample is exactly what a
  later reader mistakes for evidence.

Where a command's exact form depends on an operator choice, the shape is shown with the
placeholder in place rather than guessed. An SES endpoint hostname embeds the region,
so it appears as `feedback-smtp.<region>.amazonses.com` and not as a region someone
picked while writing documentation.

### Email provider (SES)

Covers PILOT-104 (region, domain identity, DKIM, custom MAIL FROM, DMARC, SMTP identity)
and PILOT-105 (production access, sandbox exit, bounce and complaint feedback). **Not
executed.** No AWS account, identity, IAM user, SNS topic, or DNS record described here
exists yet.

#### Region choice

The region is recorded once and then propagates into the MAIL FROM MX record, the SMTP
endpoint, and every `aws sesv2` invocation below, so choosing it late means redoing DNS.
Pick for latency to Brazil and for SES availability, record it in the PILOT-104 operator
artifact as `<region>`, and use that same string everywhere. Nothing in the repository
pins a region and nothing should.

#### Domain identity and Easy-DKIM

Create a **domain** identity for `samaronefialho.dev` (not an email-address identity;
address identities cannot carry DKIM or a custom MAIL FROM). Enable Easy-DKIM, which
yields three CNAME records of the shape:

```text
<selector1>._domainkey.samaronefialho.dev  CNAME  <selector1>.dkim.amazonses.com
<selector2>._domainkey.samaronefialho.dev  CNAME  <selector2>.dkim.amazonses.com
<selector3>._domainkey.samaronefialho.dev  CNAME  <selector3>.dkim.amazonses.com
```

Place all three in the Cloudflare zone. They must be **DNS-only** (grey cloud); a
proxied CNAME resolves to Cloudflare's edge and DKIM verification then fails against a
record that looks present. Verify by effect from a machine that is not the one that
created them:

```sh
for s in <selector1> <selector2> <selector3>; do
  dig +short CNAME "${s}._domainkey.samaronefialho.dev"
done
aws sesv2 get-email-identity --email-identity samaronefialho.dev --region <region>
```

Acceptance: three CNAMEs resolve publicly, and the identity reports verified with DKIM
signing enabled. Both halves are required. The console showing "verified" while public
DNS has not propagated is a race, not a result.

#### Custom MAIL FROM and SPF

Set the custom MAIL FROM subdomain to `mail.samaronefialho.dev`. This is what makes SPF
align with the visible sending domain instead of with an Amazon-owned bounce domain. Two
records, both DNS-only:

```text
mail.samaronefialho.dev  MX   10 feedback-smtp.<region>.amazonses.com
mail.samaronefialho.dev  TXT  "v=spf1 include:amazonses.com ~all"
```

The MX hostname embeds the region chosen above. Set the MAIL FROM behaviour on failure
to **reject** rather than to fall back to the Amazon default: a silent fallback turns an
alignment failure into mail that still sends and quietly loses its SPF alignment, which
is the failure this record exists to prevent.

#### DMARC

```text
_dmarc.samaronefialho.dev  TXT  "v=DMARC1; p=none; rua=mailto:<operator-mailbox>"
```

`p=none` is deliberate for the pilot: it reports without quarantining, so a
misconfiguration surfaces as an aggregate report rather than as invitations that never
arrive. Tightening the policy is a post-pilot decision that needs report data first, and
that data does not exist yet.

Verification for all five records at once, run **before** any of them are placed to
capture the red state, then again after:

```sh
dig +short CNAME <selector1>._domainkey.samaronefialho.dev
dig +short CNAME <selector2>._domainkey.samaronefialho.dev
dig +short CNAME <selector3>._domainkey.samaronefialho.dev
dig +short MX   mail.samaronefialho.dev
dig +short TXT  mail.samaronefialho.dev
dig +short TXT  _dmarc.samaronefialho.dev
```

Also run one deliberately wrong name and require `NXDOMAIN`. Without that control, a
resolver that wildcards the zone would make every lookup above look successful:

```sh
dig +noall +comment <deliberately-absent-name>.samaronefialho.dev
```

Red-state and NXDOMAIN control go to `.evidence/PILOT-104-red.txt` and
`.evidence/PILOT-104-failure.txt`; the post-placement outputs go to
`.evidence/PILOT-104-happy.txt` with the console statuses in `-operator.txt`.

#### A sending identity that can only send

Create a dedicated IAM user for SMTP. It exists to send raw mail and to do nothing else,
so its policy names exactly one action:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SendRawOnly",
      "Effect": "Allow",
      "Action": "ses:SendRawEmail",
      "Resource": "arn:aws:ses:<region>:<account-id>:identity/samaronefialho.dev"
    }
  ]
}
```

Generate SMTP credentials from that user. The SMTP username and password are **derived**
from the access key and a region-specific signing step, so they are not interchangeable
between regions: regenerate them if the region ever changes. Record the IAM user name,
the policy name, and the access-key id in the operator artifact. **Never record the
secret or the SMTP password anywhere in the repository or in evidence files.** They go
into `.env.prod` as `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` on the host, and nowhere
else.

#### Rotation procedure

Rotation is two-key, never delete-then-create, because the second ordering has a window
in which the stack cannot send at all:

1. Create a **second** access key on the same IAM user and derive SMTP credentials from
   it. The user now has two valid keys.
2. Replace `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` in `.env.prod` on the host and
   recreate the services that read them (`web`, `worker`, `beat`).
3. Prove the new credential sends: a `swaks` or `smtplib` probe from the box to an
   operator mailbox, delivered, headers captured.
4. Only then deactivate the old key, wait one full day so anything holding it fails
   visibly rather than silently, and delete it.

Adding a key without removing the old one is not a rotation, and removing the old one
before step 3 makes an outage out of a maintenance task.

#### Production access and sandbox exit (PILOT-105)

A fresh SES account is in the sandbox: it delivers only to verified recipients, which is
precisely the state in which every external-mailbox journey in PILOT-106 would fail.

Capture the red state first. From the box, send with the SMTP credentials to a mailbox
the operator controls but has **not** verified in SES, and record the refusal:

```sh
swaks --server email-smtp.<region>.amazonses.com:587 --tls \
      --auth-user '<smtp-username>' --auth-password '<smtp-password>' \
      --from 'no-reply@samaronefialho.dev' --to '<unverified-external-mailbox>'
```

That transcript, plus the account status page, is `.evidence/PILOT-105-red.txt`.

Request production access, then take the evidence from a command rather than from the
console, because console prose is not an assertion anyone can re-run:

```sh
aws sesv2 get-account --region <region>
```

Acceptance: the JSON reports `"ProductionAccessEnabled": true`, and the granted
`MaxSendRate` and 24-hour quota are transcribed as `<max-send-rate>` and
`<daily-quota>` in the operator artifact. Re-run the exact same `swaks` command above;
it must now deliver, and the delivered message's headers must show `DKIM=pass`,
`SPF=pass` for `mail.samaronefialho.dev`, and a `From` aligned with the domain identity.

#### Bounce and complaint feedback reaches a human

"Bounces go to the console" is not a mechanism, because nobody watches a console. Wire
an SNS topic per identity with a confirmed email subscription to an operator mailbox,
and prove the subscription is confirmed rather than merely created:

```sh
aws sns list-subscriptions-by-topic --topic-arn <bounce-topic-arn> --region <region>
```

Acceptance: the subscription's `SubscriptionArn` is a real ARN and not
`PendingConfirmation`. Record topic and subscription **names** only.

Then probe the path end to end using the SES simulator, which produces a real bounce
without harming any domain's reputation:

```sh
swaks --server email-smtp.<region>.amazonses.com:587 --tls \
      --auth-user '<smtp-username>' --auth-password '<smtp-password>' \
      --from 'no-reply@samaronefialho.dev' --to 'bounce@simulator.amazonses.com'
```

Acceptance: a bounce notification arrives in the operator mailbox. That artifact is
`.evidence/PILOT-105-failure.txt`, alongside the sandbox refusal.

**The anonymous DSR path is a monitored sender.** `POST /lgpd/dsr-submit` is reachable
without authentication, and its success path sends a notification to the encarregado.
That means an attacker, or an ordinary typo, can drive outbound mail from an unauthenticated
endpoint, and a run of bounces or complaints originating there is the earliest signal that
the endpoint is being abused. Whoever watches the bounce mailbox must know that this sender
exists and what a burst from it means. `lgpd_renotify` retries the same notification for
rows whose send failed, so a persistently bouncing encarregado address surfaces there too.
See [Credential log hygiene](#credential-log-hygiene) for what must never appear in the
logs those bursts generate.

#### Suppression list

SES keeps an account-level suppression list, and an address on it is silently not
delivered to: a journey that "sent successfully" but never arrived is the symptom.
Before declaring any PILOT-106 journey failed, check and, when the operator is certain
the address is good, remove it:

```sh
aws sesv2 get-suppressed-destination --email-address <address> --region <region>
aws sesv2 list-suppressed-destinations --region <region>
aws sesv2 delete-suppressed-destination --email-address <address> --region <region>
```

Removing an address the recipient's provider genuinely rejected re-earns the bounce and
damages the domain's reputation, so removal is a judgement call and is recorded with a
reason.

### Mailbox journeys and the log hygiene sweep

Covers PILOT-106. **Not executed.** No mailbox has received anything, no journey has been
walked, no message-id exists, and no log window has been pulled. Every value below is a
`<placeholder>` and every result is `PENDING`.

This section depends on [Production access and sandbox
exit](#production-access-and-sandbox-exit-pilot-105) having landed first. In the sandbox,
every journey to an unverified external address fails, so running these checks before
that gate measures the sandbox rather than the application.

#### Two mailboxes, and why they are different people

The journeys split across two real, externally hosted mailboxes:

| Name | Role | Used by |
| --- | --- | --- |
| `<mailbox-a>` | the firm-side user — an accountant at the pilot firm | checks 1, 2, 3, 5 |
| `<mailbox-b>` | the portal-side user — a client of that firm | check 4 |

They must be genuinely separate addresses at a provider the operator does not run, not
two aliases folding into one inbox. An alias makes a cross-audience mis-send invisible:
an invitation addressed to the portal user but rendered with the firm user's link would
land in the same place and read as success. Neither may be an SES-verified identity once
production access is granted, because a verified recipient would keep working even if the
account silently fell back into the sandbox.

#### The journey matrix

Eight numbered checks. Six are journeys, one is a negative control, one is a sweep. Each
row names what proves it, and nothing is satisfied by "the page said it was sent".

| # | Journey | What proves it |
| --- | --- | --- |
| 1 | Firm invitation | The invitation lands in `<mailbox-a>`, is accepted from that link, and the resulting user account exists and can authenticate |
| 2 | Address verification | A verification email lands in `<mailbox-a>` and the confirmation is completed by **POST**, not by following the link with a GET |
| 3 | TOTP enrolment | The firm user enrols an authenticator and completes one login with a code the operator's device generated |
| 4 | Portal invitation | The portal invitation lands in `<mailbox-b>`, is accepted, and the resulting portal user reaches that client's own Documentos list |
| 5 | Password reset | A reset email lands in `<mailbox-a>`, the reset completes, and the new password authenticates |
| 6 | LGPD DSR notification | A DSR filed at `POST /lgpd/dsr-submit` produces a notification in the encarregado mailbox **and** stamps `encarregado_notified_at` on the request row |
| 7 | **Negative control** — SMTP auth | With a deliberately wrong `EMAIL_HOST_PASSWORD`, an `smtplib` probe is refused at authentication |
| 8 | **Hygiene sweep** — logs and Sentry | Over the journey window, `docker compose logs web caddy` and the Sentry issue stream contain no raw invite token, confirm key, or reset key |

Check 2 is a POST for a reason that is easy to lose: the confirmation link is a GET-safe
landing page and the state change is the form submission on it. Recording "I clicked the
link" as the acceptance would pass against a build where the POST handler is broken.

Check 6's two halves are separate claims. The mailbox proves the message was delivered;
the `encarregado_notified_at` stamp proves the application believes it sent one. A row
with the stamp and no mail is a delivery failure the application cannot see, and a mail
with no stamp means `lgpd_renotify` will send it again.

#### Check 7 runs in a scratch shell, never against the running stack

The point of the negative control is that checks 1 through 6 succeeded because SES
authenticated the sender, not because something in the path is an open relay that would
have delivered regardless. Prove the credential is load-bearing by breaking it:

```sh
docker compose -f docker-compose.prod.yml run --rm --no-deps \
  -e EMAIL_HOST_PASSWORD=<deliberately-wrong-password> web python - <<'PY'
import os
import smtplib

try:
    with smtplib.SMTP(os.environ["EMAIL_HOST"], int(os.environ["EMAIL_PORT"])) as smtp:
        smtp.starttls()
        smtp.login(os.environ["EMAIL_HOST_USER"], os.environ["EMAIL_HOST_PASSWORD"])
except smtplib.SMTPAuthenticationError:
    print("PASS smtp auth refused with a wrong password")
    raise SystemExit(0)
print("FAIL smtp accepted a wrong password")
raise SystemExit(1)
PY
```

`run --rm --no-deps` is the whole safety property here. It starts a throwaway container
with an overridden variable and leaves `web`, `worker` and `beat` untouched, so the live
stack never holds a credential that cannot send. **Do not** edit `.env.prod` and recreate
services to produce this result: that makes the production stack unable to send mail for
the duration of the test, and a forgotten revert is an outage nobody is watching for.

The transcript goes to `.evidence/PILOT-106-failure.txt`. Record the refusal's SMTP
response class, never the password that produced it.

#### Check 8: the grep windows

This is the check most likely to be performed badly, because a grep that finds nothing
looks identical whether the logs are clean or the grep is wrong. Four things make it an
assertion rather than a gesture.

**Bound the window from timestamps taken at the journeys, not from memory.** Record UTC
immediately before check 1 and immediately after check 6, and pull exactly that span:

```sh
# Immediately before journey 1.
date -u +%Y-%m-%dT%H:%M:%SZ > /tmp/pilot-106-window-start.utc
# ... journeys 1 through 6 ...
# Immediately after journey 6.
date -u +%Y-%m-%dT%H:%M:%SZ > /tmp/pilot-106-window-end.utc
```

**Pull `web` and `caddy`, and only those two.** `web` is where Django's request and error
logging lands, and `caddy` is where the URI and Referer of every proxied request would
appear. `worker` and `beat` do not terminate HTTP and carry no credential-bearing URL,
and `db` and `redis` would only add noise. The window is applied with `--since`/`--until`
so the sweep cannot be diluted by a month of unrelated lines:

```sh
cd /opt/app-mei
compose='docker compose --env-file .env.prod -f docker-compose.prod.yml'
$compose logs --no-color --timestamps \
  --since "$(cat /tmp/pilot-106-window-start.utc)" \
  --until "$(cat /tmp/pilot-106-window-end.utc)" \
  web caddy > /tmp/pilot-106-window.log
wc -l /tmp/pilot-106-window.log
```

**Grep for the token substrings actually observed in the mailboxes.** Not a guessed
shape, not a regex for "something that looks like a token". Open each delivered message,
take the credential segment out of the link, and use a **substring** of it — enough
characters to be unique in a log, fewer than the whole value, so that the search term
itself is not the credential. There are three distinct kinds and they must be searched
separately, because they are generated by different code paths and a leak in one says
nothing about the others:

```sh
# Substrings taken from the delivered messages, entered in the operator's shell only.
: "${INVITE_TOKEN_FRAGMENT:?take from the mailbox-a invitation link}"
: "${CONFIRM_KEY_FRAGMENT:?take from the mailbox-a verification link}"
: "${RESET_KEY_FRAGMENT:?take from the mailbox-a reset link}"

for fragment in "$INVITE_TOKEN_FRAGMENT" "$CONFIRM_KEY_FRAGMENT" "$RESET_KEY_FRAGMENT"; do
  grep -c -- "$fragment" /tmp/pilot-106-window.log
done
```

**The positive control is mandatory, and it is what makes the zeros mean anything.**
Grep the same file for a benign string that is certainly present in it — a hostname, a
status code, a path with no credential in it — and require a **nonzero** count. Without
it, a mistyped path, an empty capture, a window that excluded the journeys, or a
`--since` format the daemon rejected all produce three clean zeros and an operator who
believes the logs are clean:

```sh
grep -c -- '<benign-known-present-string>' /tmp/pilot-106-window.log   # MUST be > 0
```

Acceptance for check 8: the positive control is nonzero and all three fragment counts are
zero. A zero positive control invalidates the whole sweep and the sweep is re-run; it is
never recorded as a pass with a note.

Sentry is the second half of the same check and is searched the same way. Query the issue
stream for the window and search each fragment through Sentry's own search rather than by
eye, with the same positive control — a search term known to appear in a captured event —
proving the query reaches the right project and period. Acceptance: zero events match any
fragment, and the control returns at least one.

The rotated-file variant of this sweep lives in [The canary check after
rotation](#the-canary-check-after-rotation) and runs against
`/var/lib/docker/containers/*/*-json.log*` after PILOT-209 has actually cycled files.
Same fragments, same positive control, different files: this one covers the live journey
window, that one covers what survives rotation.

#### Recording: presence, not contents

One row per journey. **No token, key, or link with its credential segment intact is ever
written into an evidence file**, including as an example, including redacted-by-eye. The
recorded link shape is the route with the credential replaced, so a reader can tell which
route was exercised without the artifact carrying a usable credential.

| Field | Recorded as |
| --- | --- |
| Journey | 1 through 6 by name |
| Recipient | `<mailbox-a>` or `<mailbox-b>`, never the literal address |
| `Message-ID` | `<message-id-N>`, transcribed to `-operator.txt` only |
| Sent at (UTC) | `<sent-utc-N>`, from the message headers |
| Received at (UTC) | `<received-utc-N>`, from the receiving provider's headers |
| DKIM | PASS / FAIL, from `Authentication-Results` |
| SPF | PASS / FAIL, and the domain it aligned to |
| Link shape | e.g. `/accounts/invitations/<token>/accept`, credential elided |
| Token present in logs | PRESENT / ABSENT, from check 8's counts |

The sent and received timestamps are both recorded because their difference is the only
delivery-latency figure the pilot will have, and because a message that was accepted by
SES and arrived forty minutes later is a different operational fact from one that arrived
immediately.

#### Evidence

| Artifact | Contents |
| --- | --- |
| `.evidence/PILOT-106-red.txt` | The pre-production-access refusal, cross-referenced to `.evidence/PILOT-105-red.txt` rather than re-run |
| `.evidence/PILOT-106-happy.txt` | Checks 1 through 6 and 8: per-journey rows, the window bounds, the line count of the captured window, the positive-control count, and the three fragment counts |
| `.evidence/PILOT-106-failure.txt` | Check 7, the SMTP authentication refusal |
| `.evidence/PILOT-106-operator.txt` | Message-ids, mailbox addresses, timestamps, and PASS/FAIL per check. No tokens, no keys, no passwords |

### Object storage: versioning, lifecycle, probe

Covers PILOT-204. **Not executed.** Versioning state, lifecycle rules, bucket name, and
the probe run below are all `PENDING`; the probe has so far run only against local
storage in PILOT-203, never against the real bucket.

#### Red state first

Capture the pre-state before changing anything, from commands rather than from the
console, so the change is provable afterwards:

```sh
aws s3api get-bucket-versioning \
  --bucket <bucket-name> --endpoint-url <oci-s3-endpoint>
aws s3api get-bucket-lifecycle-configuration \
  --bucket <bucket-name> --endpoint-url <oci-s3-endpoint>
```

Expect versioning absent or `Suspended`, and the lifecycle call to fail with a
no-such-configuration error. Both outputs go to `.evidence/PILOT-204-red.txt`.

#### Enable versioning, then prove it from a command

Enable versioning on the documents bucket. **Do not enable retention rules**: on OCI
Object Storage retention rules and versioning are mutually exclusive, so arming
retention silently costs the version history that the whole object-recovery layer
depends on. Versioning is what turns an overwrite or a delete into something
recoverable; retention would only make objects immutable for a window and would give
nothing back after an overwrite.

```sh
aws s3api get-bucket-versioning \
  --bucket <bucket-name> --endpoint-url <oci-s3-endpoint>
```

Acceptance: `Status` is `Enabled`. Console prose alone does not satisfy this; the
command output is the evidence.

#### Lifecycle: prune previous versions after 30 days

Versioning without a prune rule grows without bound, and an unbounded document bucket is
a cost incident waiting to happen. The rule targets **previous** (non-current) versions
only and never touches a current object:

```sh
aws s3api get-bucket-lifecycle-configuration \
  --bucket <bucket-name> --endpoint-url <oci-s3-endpoint>
```

Acceptance: the returned configuration contains a rule deleting non-current versions
older than 30 days, and contains **no** rule expiring current objects. Thirty days is
chosen to comfortably exceed the pilot's incident-response window while keeping the
version tail finite.

#### The versioning drill

A setting that has never been exercised is a belief. Overwrite a `_probe/` object, then
restore its prior version and verify the bytes:

```sh
aws s3api list-object-versions \
  --bucket <bucket-name> --prefix _probe/ --endpoint-url <oci-s3-endpoint>
aws s3api get-object --bucket <bucket-name> --key <probe-key> \
  --version-id <prior-version-id> --endpoint-url <oci-s3-endpoint> /tmp/restored
sha256sum /tmp/restored
```

Acceptance: the restored bytes match the pre-overwrite SHA-256, recorded as
`<pre-overwrite-sha256>` in the operator artifact. **Operate only on `_probe/` keys.**
Customer object keys are slash-free random tokens, so `_probe/` is a disjoint namespace
by construction and a drill confined to it cannot reach a customer object.

#### The probe against the real bucket

```sh
docker compose -f docker-compose.prod.yml exec -T web python manage.py storage_probe
```

The command writes, reads back with an authenticated SHA-256 comparison, requires two
anonymous GETs to be refused, deletes, and confirms absence, cleaning up in a `finally`
either way. Run it on the box. Its stdout goes to `.evidence/PILOT-204-happy.txt`.

For the failure path, run it once in a **scratch shell with a deliberately wrong**
`OCI_S3_SECRET_ACCESS_KEY`, never against the live stack's environment. Acceptance: the
auth failure is named, the exit status is nonzero, and a follow-up listing shows no
partial `_probe/` object left behind. That transcript is
`.evidence/PILOT-204-failure.txt`, and it is what proves the successful run was
authentication working rather than the bucket being open.

This section is also the destination-independent half of the
[off-host backup copy](#off-host-copies-are-provider-neutral-and-activated-only-after-the-provider-gate):
the ops bucket that receives backups is a different bucket with a different, PutObject-only
credential, and nothing here applies to it.

### Host log rotation

Covers PILOT-209. **Not executed.** `/etc/docker/daemon.json` has not been written, no
rolling restart has happened, and the flood test below has not been run.

This is **retention**, not redaction. Content redaction is a separate, already-measured
property covered by [Credential log hygiene](#credential-log-hygiene); nothing here
changes what gets written, only how much of it is kept. The 180-day
`ACCESS_LOG_RETENTION_DAYS` governs the database access log and is a different mechanism
entirely, untouched by any of this.

#### Red state

```sh
docker info --format '{{.LoggingDriver}}'
docker inspect --format '{{json .HostConfig.LogConfig}}' <container-name>
```

Expect the default `json-file` driver with an empty options map, meaning unbounded
growth. That is `.evidence/PILOT-209-red.txt`.

#### The configuration

Host-level `daemon.json` rather than `logging:` blocks in the compose files, so CI and
local development behave exactly as they do today and the host policy cannot drift into
the application's configuration:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
```

That is a 250 MB ceiling per container. The floor the values must respect is **3 days or
200 MB, whichever is larger**: retention tight enough to lose the incident window is a
worse failure than a full disk, because a disk fills loudly and a missing journey window
fails silently at exactly the moment it is needed.

Validate the file before restarting anything, and rehearse an invalid file somewhere
disposable rather than on production:

```sh
dockerd --validate --config-file /etc/docker/daemon.json
```

Acceptance for the failure path: a deliberately invalid key is rejected by that command
in the rehearsal environment. Transcript to `.evidence/PILOT-209-failure.txt`.

#### Rolling restart, because existing containers keep their old LogConfig

`systemctl restart docker` makes the daemon read the new defaults, but a container that
already exists keeps the `LogConfig` it was created with. The caps only apply to
containers **recreated** afterwards. In a maintenance window:

```sh
systemctl restart docker
cd /opt/app-mei && docker compose -f docker-compose.prod.yml up -d --force-recreate
for c in $(docker ps --format '{{.Names}}'); do
  printf '%s ' "$c"
  docker inspect --format '{{json .HostConfig.LogConfig}}' "$c"
done
```

Acceptance: **every** running container reports `max-size=50m` and `max-file=5`. A
single container still showing empty options is a container that was not recreated, and
it is the one that will fill the disk.

#### journald

The daemon's own logs are journald's problem, and journald has a separate cap:

```sh
journalctl --disk-usage
grep -E '^\s*SystemMaxUse' /etc/systemd/journald.conf
```

Record `SystemMaxUse` as `<systemmaxuse-value>`. If it is unset the default is a share
of the filesystem, which is acceptable but must be recorded rather than assumed.

#### The flood test proves rotation, on a throwaway container

Never on an application container:

```sh
docker run --rm --name log-flood-throwaway \
  --log-driver json-file --log-opt max-size=50m --log-opt max-file=5 \
  alpine sh -c 'i=0; while [ $i -lt 60 ]; do head -c 1048576 /dev/urandom | base64; i=$((i+1)); done'
```

While it runs, or immediately after, inspect the log files the daemon wrote for it and
confirm no single file exceeded the cap and that the file count did not exceed five.
Acceptance: total retained bytes for that container are bounded at roughly 250 MB
despite ~60 MB per pass being emitted well past that. Transcript to
`.evidence/PILOT-209-happy.txt`.

#### The canary check after rotation

Rotation must not be the thing that makes a credential visible. After the rotation has
actually cycled files, grep the rotated files for the token substrings observed during
the PILOT-106 mailbox journeys and require zero hits, with a benign known-present string
as the positive control proving the grep could have seen a leak at all:

```sh
grep -c '<benign-known-present-string>' /var/lib/docker/containers/*/*-json.log*
grep -c '<observed-token-substring>'     /var/lib/docker/containers/*/*-json.log*
```

Acceptance: positive control nonzero, token substrings zero. Record counts only; never
copy a token into an evidence file.

This procedure is repeated verbatim on the target host as part of
[New host bootstrap](#new-host-bootstrap-two-phases), Phase A for the configuration and
Phase B for the re-verification.

### Host requirements

Covers PILOT-401. **Not executed.** No provider has been chosen, no instance ordered, and
no shape below has been measured against a real host.

The move is like-for-like. Nothing here selects managed Kubernetes or a PaaS, and nothing
here changes the architecture; the target runs the same compose stack the current box
runs.

| Requirement | Minimum | Recommended | Why |
| --- | --- | --- | --- |
| vCPU | 1 | 2 | The current box is an OCI E2.1.Micro with 1 OCPU and the stack runs, but backups are niced to idle precisely because one core cannot serve and back up at once |
| RAM | 1 GiB **plus swap** | 2 GiB | The current box has 954 MiB plus 4 GiB of swap; the container memory limits total roughly 1.5 GiB nominal, so without swap the 1 GiB shape is not survivable |
| Disk | 40 GiB | 40 GiB or more | The deploy preflight refuses to land an image pair with less than 3 GiB free under the docker data root, and the WAL archive accrues on the same filesystem |
| OS | Ubuntu LTS | Ubuntu LTS | systemd units are shipped as-is; an image without systemd invalidates the bootstrap |
| Network | 80, 443, SSH inbound | same | No provider-level firewall may block them; Caddy needs 80 and 443, CI needs SSH |
| Region | acceptable latency to Brazil | same | Users and the SES region should not disagree by an ocean |
| Access | root or sudo-capable SSH | same | Bootstrap installs packages and writes `/etc/docker/daemon.json` and `/etc/systemd/system` |

Swap deserves emphasis because it is the requirement a provider's marketing page will not
mention. A 1 GiB instance with no swap will OOM-kill a container during a migration or a
base backup, and the failure looks like an unrelated crash.

#### Acceptance transcript

One command, run from the operator's machine, recorded verbatim:

```sh
ssh <target-host> 'uname -a && free -m && df -h /'
```

Acceptance: the OS line matches the chosen image, `free -m` shows the RAM **and** a
non-zero swap total, and `df -h /` shows at least 40 GiB with enough free for the 3 GiB
deploy headroom plus the archive. Transcript to `.evidence/PILOT-401-happy.txt`, with
provider, instance shape, region, OS image, and public IP recorded as `<provider>`,
`<shape>`, `<region>`, `<os-image>`, `<target-ip>` in `-operator.txt`.

Any UNMET row blocks the bootstrap and is recorded in `.evidence/PILOT-401-failure.txt`
rather than worked around. A host that does not meet the sheet will fail during the
cutover window, which is the worst possible time to discover it.

### New host bootstrap (two phases)

Covers PILOT-402. **This section is the runsheet, pre-execution.** No host has been
bootstrapped. Once the procedure has actually been executed end to end, this section is
replaced by the as-executed version written from the resulting transcripts; until then
every step below is a proposal that has never met a real machine.

The split into two phases exists because the deadline lane needs a base host early, and
the finalizing values do not exist until later waves land. Phase A depends on
[Host requirements](#host-requirements) and nothing else. Phase B depends on PILOT-106
for the final `EMAIL_*` values, PILOT-208 for `HC_URL`, PILOT-209 for the rotation
re-verification, and PILOT-301 for the off-host credentials.

**The stack is not started in either phase.** The first deploy belongs to PILOT-403.

#### Two rules that apply to both phases

**Never run `ops/systemd/install.sh` on the target.** Its last action is
`systemctl enable --now app-mei-backup.timer`, unconditionally. Running it during
bootstrap arms the nightly backup on a host with no data, pointing at credentials that
may still be `CHANGEME`, and the resulting failures teach the operator to ignore the
alarm. Units are copied manually and left disabled. Step 8 of
[Cutover: data freeze, restore, and DNS flip](#cutover-data-freeze-restore-and-dns-flip)
is the single point at which the timer is enabled, after the data is actually on the box.

**The target uses the real ACME configuration from the start.** Never `tls internal`.
`TLS_DIRECTIVE` stays empty and `ACME_DNS_OPTION=acme_dns cloudflare {env.CF_API_TOKEN}`
with a `CF_API_TOKEN` scoped to Zone → DNS → Edit on this zone only. The DNS-01 challenge
issues the wildcard certificate through a TXT record, so it never needs the A record to
point at the target: the target therefore holds a browser-valid certificate **before**
cutover. That is exactly what makes the deploy job's pinned `--resolve` assertions and
the pre-flip verification in PILOT-404 possible, and a self-signed placeholder would
break both. Note that compose reads `${TLS_DIRECTIVE-tls internal}`, which substitutes
the default only when the variable is **unset**, so the deliberate empty value must be
present in `.env.prod` and not merely omitted.

#### Phase A: base host

Nothing in Phase A depends on Waves 1 through 3.

1. **Docker and the compose plugin.** Install from the distribution's or Docker's
   official repository. Verify: `docker --version && docker compose version`.
2. **Deploy user and key.** Create the account CI will connect as, install the
   authorized key, and confirm it can reach the docker socket. Verify:
   `ssh <deploy-user>@<target-ip> 'docker ps'` returns without a permission error.
3. **Checkout.** `git clone` the repository at the deploy path on `main`. Verify:
   `git -C <deploy-path> rev-parse HEAD` and `git -C <deploy-path> status --short`.
4. **Log rotation.** Apply `/etc/docker/daemon.json` with the decided 50m × 5 values and
   restart the daemon, exactly as in [Host log rotation](#host-log-rotation). Verify with
   a throwaway container's `LogConfig`, since no application container exists yet.
5. **`.env.prod` skeleton.** Copy `.env.prod.example`. Set the ACME variables per the
   rule above. Every value that is not yet known, meaning `EMAIL_*`, `HC_URL`, and the
   off-host credentials, is written as a literal `CHANGEME` so that a missed value is a
   grep away rather than an empty string that compose would happily accept.
6. **systemd units, copied and left inert.**

   ```sh
   cp <deploy-path>/ops/systemd/app-mei-backup.service /etc/systemd/system/
   cp <deploy-path>/ops/systemd/app-mei-backup.timer   /etc/systemd/system/
   systemctl daemon-reload
   ```

   No `enable`. No `start`. No `install.sh`.

7. **Prove the timer is inert**, which is the assertion that catches an accidental
   `enable`:

   ```sh
   systemctl is-enabled app-mei-backup.timer   # expect: disabled
   systemctl is-active  app-mei-backup.timer   # expect: inactive
   journalctl -u app-mei-backup.service -n 5   # expect: zero invocations
   ```

Red state for Phase A is the pre-bootstrap transcript showing `docker --version` absent
and the checkout missing, to `.evidence/PILOT-402-red.txt`.

#### Phase B: pre-cutover finalization

Run after PILOT-106, 208, 209, and 301, and before PILOT-403.

1. **Refresh the checkout first.** This step is not optional and its position is not
   arbitrary. Phase A copied the units from a pre-Wave-2 checkout, and PILOT-208
   subsequently added `EnvironmentFile=-/etc/app-mei/backup.env` to
   `app-mei-backup.service`. Enabling the stale unit at cutover would run the backup with
   no monitoring and no off-host credentials, succeeding locally and signalling nothing.

   ```sh
   git -C <deploy-path> fetch --prune origin
   git -C <deploy-path> reset --hard origin/main
   ```

2. **Re-copy both units and reload**, then verify the unit parses:

   ```sh
   cp <deploy-path>/ops/systemd/app-mei-backup.service /etc/systemd/system/
   cp <deploy-path>/ops/systemd/app-mei-backup.timer   /etc/systemd/system/
   systemctl daemon-reload
   systemd-analyze verify /etc/systemd/system/app-mei-backup.service
   ```

   `install.sh` remains forbidden here for the same reason as in Phase A.

3. **Re-prove inertness**, the same three assertions as Phase A step 7. Re-copying a unit
   does not enable it, but the assertion costs nothing and the failure it catches is a
   backup running against a half-configured host.

4. **Replace every `CHANGEME`.** The check is a sorted-key diff against the example file,
   which catches both a missing variable and a placeholder left behind:

   ```sh
   diff <(grep -oE '^[A-Z_]+=' <deploy-path>/.env.prod.example | sort) \
        <(grep -oE '^[A-Z_]+=' <deploy-path>/.env.prod        | sort)
   grep -c CHANGEME <deploy-path>/.env.prod
   ```

   Acceptance: the diff is empty and the `CHANGEME` count is zero. Values are never
   recorded in evidence, only the fact that the diff was empty.

5. **Place the backup environment file** at `/etc/app-mei/backup.env` with `HC_URL`,
   `HC_OFFHOST_URL`, and the off-host credentials described under
   [Off-host copies](#off-host-copies-are-provider-neutral-and-activated-only-after-the-provider-gate).
   Confirm the path matches the refreshed unit's `EnvironmentFile=` line; the leading `-`
   makes the file optional, which protects the backup from a missing monitoring secret but
   equally means a **wrong path fails silently**. Root-owned, mode 0600.

6. **Re-run the PILOT-209 verification** on this host: `LogConfig` on every container
   that exists, and the journald cap.

7. **Timer still disabled.** Re-transcribe `systemctl is-enabled app-mei-backup.timer`
   as `disabled`. Step 8 of
   [Cutover: data freeze, restore, and DNS flip](#cutover-data-freeze-restore-and-dns-flip)
   is the sole enable point.

For the failure path, deliberately omit one variable from `.env.prod`, attempt a compose
config parse, and capture the `${VAR:?}` refusal message before adding it back. That
refusal is the guard that makes an incomplete Phase B unable to reach a running stack.
Transcript to `.evidence/PILOT-402-failure.txt`; the per-step transcripts go to
`.evidence/PILOT-402-happy.txt`.

### Cutover: data freeze, restore, and DNS flip

Covers PILOT-404. **Not executed.** No target host holds production data, no DNS record
has been changed, no freeze window has been scheduled, and every measured value below is
a `<placeholder>`.

This is the most dangerous procedure in the pilot, and the reason is not that it is
complicated. It is that its two principal failure modes produce a system that looks
completely healthy.

**Cutting over from a live-written source silently loses rows.** If the write freeze in
step 2 is skipped or is incomplete, every row committed on the old box after the final
backup is simply absent on the target. Nothing errors, no log line records it, and
`/healthz` is green. The gap is found weeks later by a user who cannot locate a document
they know they uploaded, at which point the old box may already be gone.

**Restoring from an archive that stops short of the freeze point silently truncates
history.** WAL is archived per segment, and `archive_timeout=300` forces a switch every
five minutes, so at the instant the last write lands the tail of the log is still sitting
in the active segment and has never reached the archive. If step 3 does not force a
segment switch and then wait for the archive queue to drain, PITR-to-end replays only
what the archive happens to hold. Recovery reports success. It is short.

Neither failure raises an alarm anywhere in the stack. The only thing standing between
them and production is step 7's row-count comparison against counts captured on the old
box **after** the freeze, which is why step 7 is not optional and why the stale-count
control below exists.

#### Standing rules for the window

- **MUST NOT skip the write freeze.** See above. A "quick" cutover without it is a
  cutover that loses data it cannot name.
- **MUST NOT disable HSTS, and MUST NOT serve any non-TLS window.**
  `SECURE_HSTS_SECONDS` is one year (`config/settings/prod.py:221-223`), so every browser
  that has visited the site will refuse plaintext for a year regardless of what the
  server later says. A single non-TLS moment is not a degraded experience, it is an
  outage for returning visitors that no server-side change can shorten. The DNS-01
  wildcard the target already holds (see the TLS rule under
  [New host bootstrap](#new-host-bootstrap-two-phases)) exists precisely so this
  constraint costs nothing.
- **MUST NOT lower the DNS TTL below 300s permanently.** 300s for the window only,
  restored in step 12.
- **MUST NOT destroy old-box data.** It is the rollback anchor until `v0.2.0-rc1` exists.
  See [Decommissioning the old host](#decommissioning-the-old-host).

#### The ordered state machine

The order is the procedure. A step run out of sequence is not a slower cutover, it is a
different and usually silent outcome. Every step is transcribed to
`.evidence/PILOT-404-happy.txt` as it is executed.

| # | Step | Its check |
| --- | --- | --- |
| 1 | Lower TTL to 300s, at least one hour before the window | `dig +noall +answer samaronefialho.dev` reports `300` |
| 2 | Freeze the source: caddy, then beat, then drain, then worker | Drain counters both zero, transcribed |
| 3 | Final backup, forced WAL switch, archive drain | Zero `.ready` files, `failed_count` unchanged |
| 4 | Transfer base, WAL archive, and logical dump old host to target | SHA-256 match on both ends |
| 5 | Reset target state: `down`, remove three volumes, quarantine target-made artifacts | `docker volume ls` shows none of the three; listing transcribed |
| 6 | Restore on target per RESTORE.md, then `up -d db redis web caddy` | Recovery confirmed, worker and beat absent from `docker compose ps` |
| 7 | Verify on target **before** the flip | Five checks, all green, including row counts |
| 8 | `up -d worker beat`, then enable the backup timer | `systemctl is-enabled` reports `enabled` |
| 9 | Flip A/AAAA apex and wildcard to the target | Cloudflare shows the new values |
| 10 | Verify publicly, unpinned | Target IP answers, correct issuer, `/versionz` equals `main` |
| 11 | Old box stays stopped but intact | Volumes present, caddy down, freeze still in force |
| 12 | Restore the TTL | `dig` reports the pre-window value |

##### 1. Lower the TTL, at least an hour ahead

In Cloudflare, set the apex `A`, any `AAAA`, and the wildcard record to a 300s TTL. Do
this **at least one hour before** the window opens so that resolvers holding the old,
longer TTL have expired their cached copies before the flip matters. Lowering the TTL at
the same moment as the flip achieves nothing: the caches that need to expire were
populated under the old value.

```sh
dig +noall +answer samaronefialho.dev A
dig +noall +answer '*.samaronefialho.dev' A
```

Record the pre-window TTL as `<pre-window-ttl>`; step 12 restores exactly that value.

##### 2. Freeze the source, in this exact order

The order closes each write path before the one that feeds it, so nothing is abandoned
mid-flight.

```sh
cd /opt/app-mei
compose='docker compose --env-file .env.prod -f docker-compose.prod.yml'

# a. Public ingress first. No new HTTP writes can arrive after this.
$compose stop caddy

# b. No new scheduled tasks can be enqueued after this.
$compose stop beat

# c. DRAIN. Both counters must reach zero before the worker is stopped.
$compose exec -T worker celery -A config inspect active
$compose exec -T worker celery -A config inspect reserved
$compose exec -T worker celery -A config inspect scheduled
$compose exec -T redis redis-cli -h 127.0.0.1 llen celery

# d. Only now.
$compose stop worker
```

Acceptance for the drain: `active`, `reserved` and `scheduled` all report empty lists
for every worker, **and** `llen celery` returns `0`. Both halves are required and they
answer different questions. The `inspect` calls describe what the worker holds in
memory; `llen` describes what is still queued in Redis waiting for a worker that is
about to stop existing. Transcribe all four outputs verbatim, because "the drain looked
clean" is not a check.

`celery -A config` is the correct invocation here: the app object lives at
`config/celery.py` and is re-exported from `config/__init__.py`, which is the same form
the container healthchecks use (`docker-compose.prod.yml:290`).

Stopping `caddy` before `beat` matters more than it looks. Reverse them and a scheduled
task can still be enqueued while HTTP writes are also arriving, so the queue keeps
growing while you are trying to empty it.

##### 3. Final backup, forced WAL switch, and archive drain

```sh
sudo /opt/app-mei/ops/backup.sh
```

Acceptance: exit status `0`, a fresh base and logical dump under `ops/backups/`, the
freshness marker stamped, and both Healthchecks pings green. The script writes the local
freshness marker at `ops/backup.sh:226` and only then starts the off-host copy at
`ops/backup.sh:268`, so a green marker with a failed off-host copy means the local
artifacts are complete and the remote copy needs re-running, not that the backup is
suspect.

The base backup alone is not sufficient, and this is the step that is easy to skip
because everything already looks finished. Force the switch and wait:

```sh
$compose exec -T -u postgres db psql -U "$POSTGRES_USER" -d app_mei -XAt \
  -c "SELECT pg_switch_wal();"

# Wait until this reports 0. Re-run until it does.
$compose exec -T -u postgres db sh -ceu \
  'ls -1 "$PGDATA"/pg_wal/archive_status/*.ready 2>/dev/null | wc -l'

# And confirm the archiver did not start failing while draining.
$compose exec -T -u postgres db psql -U "$POSTGRES_USER" -d app_mei -XAt \
  -c "SELECT archived_count, failed_count, last_archived_wal FROM pg_stat_archiver;"
```

Acceptance: zero `.ready` files, and `failed_count` identical to the value read before
the switch. Record both readings, not just the second, because "failed_count is 2" means
nothing without the prior value. Record `last_archived_wal` as `<final-archived-wal>`:
that segment name is the proof of what the archive actually contains, and step 6's
recovery is expected to reach it.

A rising `failed_count` here stops the cutover. It means the archive does not contain
what the restore is about to assume it contains, and the correct response is to fix
archiving on the old box, which is still intact and still frozen, rather than to proceed
and discover the gap on the target.

##### 4. Transfer directly, host to host

```sh
BASE=<final-base-directory-name>
rsync -av --checksum \
  /opt/app-mei/ops/backups/base/"$BASE"/ \
  <deploy-user>@<target-ip>:/opt/app-mei/ops/backups/base/"$BASE"/
rsync -av --checksum \
  /opt/app-mei/ops/backups/logical/ \
  <deploy-user>@<target-ip>:/opt/app-mei/ops/backups/logical/
```

The WAL archive is a docker volume rather than a host path, so it moves as a stream
rather than as a directory copy. Take it from the old box and land it on the target
using the same volume name:

```sh
# On the old box.
docker run --rm -v app-mei_wal_archive:/wal -w /wal alpine tar -cf - . \
  | gzip -1 > /tmp/wal_archive.tar.gz
sha256sum /tmp/wal_archive.tar.gz
scp /tmp/wal_archive.tar.gz <deploy-user>@<target-ip>:/tmp/
```

Acceptance: SHA-256 of every transferred artifact matches on both ends, compared
explicitly rather than assumed from a green `rsync`. The off-host bucket copy from step 3
runs as usual and is not a substitute for this transfer: it is the disaster copy, this is
the migration path.

##### 5. Reset the target's state, deliberately and completely

PILOT-403 proved the deploy pipeline against a fresh, empty-but-migrated database on the
target. **All of that state is discarded here**, and each of the three volumes has its own
reason:

- `postgres_data` holds the pipeline-test cluster. Restoring the source base backup over
  a populated data directory is not a restore, and the RESTORE.md procedure requires the
  volume to be absent so that the image entrypoint skips `initdb` and enters archive
  recovery.
- `wal_archive` is the one people forget, and it is the one that corrupts the result.
  WAL segment names are a function of timeline and LSN, not of which cluster produced
  them, so the target's own segments **collide by name** with the source's. Recovery would
  then read a mixture of two unrelated clusters' WAL, and the `restore_command` has no way
  to notice.
- `redis_data` holds whatever tasks the pipeline test queued. Leaving it means step 8
  starts a worker that immediately executes test-run tasks against real production data.

```sh
cd /opt/app-mei
$compose down
docker volume rm app-mei_postgres_data app-mei_wal_archive app-mei_redis_data
docker volume ls --filter name=app-mei
```

Acceptance: none of the three names appears in the final listing. Note this is a
deliberate, named removal and **not** `down -v`, which would also take `caddy_data` and
discard the ACME account and certificate material the target already holds
(`docker-compose.prod.yml:392-397`).

Then quarantine any backup artifact the target produced for itself, locally and off-host.
A target-made base backup sitting beside the source's is an artifact that will restore
cleanly to the wrong data:

```sh
ls -la /opt/app-mei/ops/backups/base /opt/app-mei/ops/backups/logical
aws s3 ls "s3://$OFFHOST_S3_BUCKET/pg/" --endpoint-url "$OFFHOST_S3_ENDPOINT"
```

Transcribe both listings before and after, and record which entries were removed with
their timestamps. An artifact whose timestamp falls between the PILOT-403 deploy and this
step is target-made by construction.

##### 6. Restore on the target, then start only four services

Follow [`Path A — physical restore with WAL replay`](RESTORE.md#path-a--physical-restore-with-wal-replay-rehearsed)
exactly as rehearsed, restoring the transferred base into `app-mei_postgres_data` and the
transferred archive into `app-mei_wal_archive`, recovering to the end of the WAL. The
roles step is [`Roles recreation`](RESTORE.md#roles-recreation); `ops/sql/roles.sql`
guards every `CREATE ROLE` with an `IF NOT EXISTS` catalog check
(`ops/sql/roles.sql:27-37`), so re-applying it against a restored cluster that already
carries the roles is safe.

Bring up **four** services and no more:

```sh
$compose up -d --no-build --wait db redis web caddy
$compose ps
```

Acceptance: `db`, `redis`, `web` and `caddy` are healthy, and `worker` and `beat` do not
appear. Starting the worker here would begin executing tasks against data whose row
counts have not yet been verified, before there is any decision to keep this restore.
Confirm recovery reached the expected point per
[`Confirm recovery really happened`](RESTORE.md#confirm-recovery-really-happened), and
check the recovered position against `<final-archived-wal>` from step 3.

##### 7. Verify on the target, before the flip

Five checks. Public DNS still points at the old box throughout, so every HTTP check pins
resolution the same way the deploy job's assertions do.

```sh
target_ip="$(getent ahostsv4 <target-host> | awk 'NR==1{print $1}')"
```

1. **Migrations, positive-controlled.** Not "no pending", but "at least one applied and
   none pending", because a probe that dies emits nothing and nothing contains no pending
   marker.

   ```sh
   $compose exec -T web sh -c \
     'DATABASE_URL="$DATABASE_MIGRATION_URL" python manage.py showmigrations --plan --skip-checks' \
     > /tmp/plan.txt
   grep -c '\[X\]' /tmp/plan.txt      # >= 1
   ! grep -q '\[ \]' /tmp/plan.txt    # no pending
   ```

2. **RLS spot check.** With `app.tenant_id` empty, a session running as `app_runtime`
   must see zero rows. This is the property the whole isolation model rests on, and a
   restore that silently landed tables without their policies would pass every other
   check here.

3. **Row counts match the old box's post-freeze counts.** Capture the counts on the old
   box after the freeze, into a file, and compare. Table names are
   `tenants_tenant`, `clients_clientcompany` and `obligations_document`:

   ```sh
   # On the OLD box, after step 2's freeze:
   $compose exec -T -u postgres db psql -U "$POSTGRES_USER" -d app_mei -XAt -F'|' -c \
     "SELECT 'tenants', count(*) FROM tenants_tenant
      UNION ALL SELECT 'clients', count(*) FROM clients_clientcompany
      UNION ALL SELECT 'documents', count(*) FROM obligations_document
      ORDER BY 1;" | tee /tmp/expected-counts.txt
   sha256sum /tmp/expected-counts.txt

   # On the TARGET, after the restore, the identical query:
   ... | tee /tmp/actual-counts.txt
   diff /tmp/expected-counts.txt /tmp/actual-counts.txt
   ```

   Acceptance: `diff` is empty. This is the only check in the entire procedure that can
   detect either of the two silent failure modes described at the top of this section.
   Treat a mismatch as "the freeze or the archive drain was incomplete", not as "the
   counts drifted".

4. **Canary document.** Fetch one known document through the target and compare its
   SHA-256 against the value recorded on the old box. Row counts prove the metadata
   arrived; only a byte-level fetch proves the document path works end to end from the
   restored rows to the object store. Use the identifiers from the secure rehearsal
   manifest rather than writing any id into evidence, following
   [`Restored-data and RLS-by-effect evidence`](RESTORE.md#restored-data-and-rls-by-effect-evidence).

5. **Pinned health and version.**

   ```sh
   curl -fsS --max-time 30 --resolve "samaronefialho.dev:443:$target_ip" \
     "https://samaronefialho.dev/healthz?cb=<window-id>" | jq -e '.status == "ok"'

   curl -fsS --resolve "samaronefialho.dev:443:$target_ip" \
     "https://samaronefialho.dev/versionz?cb=<window-id>" \
     | jq -e --arg SHA "<main-head-sha>" '.release == $SHA'
   ```

   `jq -e` is what makes these bite: a 200 carrying the wrong body exits nonzero rather
   than scrolling past. Note `/healthz` will report `"backup": "stale"` here, because the
   restored marker is the old box's and the target's timer is still disabled. That is
   expected and does not change `status`; it clears after step 8's first timer run.

Any failing check stops the cutover **before** anything public has changed. That is the
entire point of doing all five here rather than after the flip: at this moment the old
box is still serving, still intact, and the rollback is "do nothing".

##### 8. Start the remaining services, and enable the timer

```sh
$compose up -d --no-build --wait worker beat
systemctl enable --now app-mei-backup.timer
systemctl is-enabled app-mei-backup.timer   # expect: enabled
systemctl list-timers app-mei-backup.timer --no-pager
```

**This is the single point in the entire plan at which the backup timer is enabled.**
PILOT-402 Phase A step 7 and Phase B step 7 both assert it is `disabled`, deliberately,
because a timer armed on a host with no data teaches the operator to ignore its alarms.
It is enabled here and not one step earlier because a backup of an unverified restore is
an artifact nobody should be tempted to trust.

Use `systemctl enable --now` directly. `ops/systemd/install.sh` is still forbidden on
this host for the reason given under
[New host bootstrap](#new-host-bootstrap-two-phases): it enables unconditionally and
would have been just as happy to do so at bootstrap.

##### 9. Flip DNS

In Cloudflare, repoint the apex `A` (and `AAAA` if one exists) and the wildcard record to
`<target-ip>`. Record the previous values as `<old-ip>` before changing them; step 12's
rollback path is these exact values and nothing else. Record the flip timestamp as
`<flip-utc>`.

##### 10. Verify publicly, unpinned

Everything here deliberately drops the `--resolve` pin, because the question has changed
from "does the target serve correctly" to "does the name now reach the target".

```sh
dig +short A samaronefialho.dev
dig +short A '*.samaronefialho.dev'

# The connection line names the address actually used.
curl -fsSI -v https://samaronefialho.dev/healthz 2>&1 | grep -i 'Connected to'

curl -fsS https://samaronefialho.dev/healthz | jq -e '.status == "ok"'
curl -fsS https://samaronefialho.dev/versionz | jq -e --arg SHA "<main-head-sha>" '.release == $SHA'
curl -fsSI https://<portal-host>/ | head -1

echo | openssl s_client -connect samaronefialho.dev:443 -servername samaronefialho.dev 2>/dev/null \
  | openssl x509 -noout -issuer -ext subjectAltName
```

Acceptance: `dig` returns `<target-ip>`; the `Connected to` line names `<target-ip>`; the
certificate issuer is Let's Encrypt and its SANs cover both the apex and the wildcard;
`/versionz` equals `main`; the portal host serves. UptimeRobot monitors are hostname
based, so they follow the flip on their own: confirm all of them report UP rather than
reconfiguring anything.

##### 11. The old box stays stopped but intact

Its `caddy` is already down from step 2, its worker and beat are stopped, and its
database is frozen at the backup point. **Leave every volume in place.** While that is
true, the deep rollback is available and cheap. Nothing about the old box changes until
the rollback decision is explicitly closed and recorded, and its eventual shutdown and
destruction belong to [Decommissioning the old host](#decommissioning-the-old-host),
not to this window.

##### 12. Restore the TTL, and record the rollback path

Set the apex and wildcard TTLs back to `<pre-window-ttl>` and confirm with `dig`.

The rollback path, valid for as long as step 11 holds:

1. Repoint the `A`, `AAAA` and wildcard records back to `<old-ip>`.
2. `docker compose up -d caddy` on the old box.
3. Wait out the 300s TTL, then re-run step 10's checks expecting `<old-ip>`.

Its cost must be recorded in the window log rather than discovered: the old box's data is
at the freeze point, so **every write the target accepted after `<flip-utc>` is lost by
this rollback**. Record `<flip-utc>`, the rollback decision time, and the elapsed window
between them, because that interval is exactly the quantity of data at stake and it is
what makes the choice between rolling back and rolling forward an informed one rather
than a reflex.

#### The two negative controls

Both are run during the window and both go to `.evidence/PILOT-404-failure.txt`.

**Control 1: the rollback target answers.** During the window, before the old box's stop
is final, pin a request at `<old-ip>` and confirm it is served:

```sh
curl -fsS --max-time 30 --resolve "samaronefialho.dev:443:<old-ip>" \
  "https://samaronefialho.dev/healthz" | jq -e '.status == "ok"'
```

Acceptance: 200 with `"status": "ok"` from `<old-ip>`. This is not a duplicate of step
10. Step 10 proves the new edge works; this proves the rollback in step 12 is a real
option rather than a paragraph, and it is worth exactly nothing after the old box has
been touched, which is why it runs inside the window.

To run it the old box's `caddy` must be briefly up while its worker, beat and database
writes stay frozen. Starting `caddy` alone does not unfreeze anything: the freeze is
that no writer is running, not that the edge is closed.

**Control 2: the row-count check bites.** A comparison that has never failed is not yet
a check, it is a command that has always printed nothing. Prove it fails before trusting
it:

```sh
cp /tmp/expected-counts.txt /tmp/expected-counts.bak
sha256sum /tmp/expected-counts.bak

# Alter exactly one count.
sed -i '0,/^documents|/{s/^documents|\([0-9]*\)$/documents|999999/}' /tmp/expected-counts.txt
diff /tmp/expected-counts.txt /tmp/actual-counts.txt   # MUST report the documents line

# Restore and re-prove.
cp /tmp/expected-counts.bak /tmp/expected-counts.txt
sha256sum /tmp/expected-counts.txt                     # matches the pre-mutation hash
diff /tmp/expected-counts.txt /tmp/actual-counts.txt   # empty again
```

Acceptance: the mutated run fails **and names the `documents` line**, the restored file's
SHA-256 equals the recorded pre-mutation value, and the re-run is clean. A mutation that
fails without naming which count moved would leave the operator unable to act on a real
mismatch, so the naming is part of the acceptance and not incidental.

#### Evidence

| Artifact | Contents |
| --- | --- |
| `.evidence/PILOT-404-red.txt` | `dig +short A samaronefialho.dev` returning `<old-ip>`, with the old box serving |
| `.evidence/PILOT-404-happy.txt` | The 12-step transcript, per step, including the drain counters, the WAL-switch wait, the volume-reset listing, and all five step-7 checks |
| `.evidence/PILOT-404-failure.txt` | Both negative controls |
| `.evidence/PILOT-404-operator.txt` | `<target-ip>`, `<old-ip>`, `<flip-utc>`, `<pre-window-ttl>`, window duration, and PASS/FAIL per step. No secrets, no document ids |

### Decommissioning the old host

Covers PILOT-406. **Not executed.** Nothing has been re-verified from a target host, and
no host has been decommissioned.

**The old box is the deep-rollback anchor and must not be decommissioned before
`v0.2.0-rc1` exists.** Every check below is about earning the right to stop it, not about
stopping it.

#### Re-verification checklist, all from the target

Everything proven on the old box is proven again from the new one, because evidence
gathered on a host that is about to be switched off is evidence about the wrong machine.

| # | Check | Acceptance |
| --- | --- | --- |
| 1 | One email journey per type: firm invite, address verification, password reset, LGPD DSR notification | Each delivered to a real external mailbox, message-ids recorded, DKIM and SPF pass, `From` aligned |
| 2 | Backup timer fires once end to end | Local base and logical artifacts present with a fresh marker, off-host copy landed under `pg/<UTC-timestamp>/`, both Healthchecks checks green. This is the **authoritative** off-host evidence; the local MinIO rehearsal never was |
| 3 | `verify-live` workflow green against the target | Scheduled or dispatched run passes, with `/versionz` release equal to `main` |
| 4 | UptimeRobot | All monitors UP with their JSON assertions passing |
| 5 | Log rotation on the target | The [Host log rotation](#host-log-rotation) checks re-run: `LogConfig` on every container, journald cap recorded |
| 6 | Sentry receives a target-origin event | The event's `release` field equals the deployed SHA |

Any failing row blocks Wave 5 and its remediation is recorded in
`.evidence/PILOT-406-failure.txt` rather than retried until it passes without explanation.
Row 2 in particular is the one that has never had authoritative evidence.

The off-host copy check has an independent verification half worth stating explicitly: a
**separate read credential** downloads both artifacts and compares byte count and SHA-256
against the local sources. The backup identity is PutObject-only by design and cannot
list or read, so it cannot verify its own work.

#### Decommission plan

Executed only after `v0.2.0-rc1` exists and rows 1 through 6 are green.

1. **What leaves the old box, and when.** Stop `caddy` first so the old box cannot serve
   the public edge even if DNS regresses, then the remaining services. Leave the docker
   volumes intact. Record the stop timestamp as `<old-box-stop-utc>`.
2. **Data retained.** Postgres data, the WAL archive, and the local backup artifacts stay
   on the old box for `<retention-days>` after RC, then the instance is destroyed. The
   retention window is a decision recorded in the operator artifact, not a default.
3. **Evidence retained.** The final backup taken during the
   [cutover freeze](#cutover-data-freeze-restore-and-dns-flip), its off-host
   copy, and the cutover window log are kept independently of the instance's lifetime,
   because they outlive the machine that produced them.
4. **Secrets rotated at cutover, names only.** Rotate rather than migrate: a credential
   that lived on a host being decommissioned should not be the credential guarding the
   new one. The list, recorded by name with values never written down here, is the deploy
   SSH key, the SES SMTP credential (via the two-key procedure above), the Cloudflare API
   token, the document-bucket credential, the off-host ops-bucket credential, and both
   Healthchecks ping URLs. Each rotation is verified by effect before the previous value
   is deleted.
5. **Rollback stays available until the last moment.** While the old box exists with its
   volumes intact, the deep rollback is repointing DNS and restarting its Caddy. Destroying
   the instance is the step that ends that option, so it is deliberately the last one and
   it is dated.

### Alerting drills: Sentry, the degraded signal, and the missed ping

Covers PILOT-504. **Not executed.** No Sentry event has been raised deliberately, no
service has been stopped to provoke an alert, no ping has been skipped, and no alert has
been received. Every event id, timestamp, and elapsed value below is a `<placeholder>`.

Three monitoring surfaces are configured by earlier todos and none of them has ever
fired. A configured alert that has never been received is a belief about a delivery chain
with at least four independent links in it: the condition, the detector, the notifier,
and the recipient's mail. This section fires each one on purpose and records what
arrived, because the first real incident is the wrong time to discover that the alert
address was wrong.

All three parts run inside a **declared maintenance window** on the target box, announced
to the pilot firm in advance. Part (b) deliberately makes `/healthz` fail, which means an
external monitor will page, so the window must also be recorded wherever those alerts
land — an unexplained alert during a drill teaches the recipient to ignore the next one.

#### Non-goals: what this drill does not break

**Do not drill by stopping Redis or Postgres.** Both are tempting because both would
produce loud alerts quickly, and both are the wrong choice:

- Stopping `db` takes every request down, not just the probe, and the recovery crosses a
  restore-shaped decision on a box holding real pilot data.
- Stopping `redis` empties the Celery broker and the rate-limit buckets, so recovery is
  not the inverse of the action and the blast radius outlives the window.

`beat` is chosen because the scheduler dead-man is a **designed** signal rather than a
side effect of a broken dependency. Its failure mode is bounded — no HTTP request path
depends on beat — the alert it raises is the one the runbook already has an entry for,
and the recovery is one `docker compose start`. Drilling the designed detector proves the
detector; breaking a dependency proves only that breaking dependencies is noticeable.

**Do not leave any drill state active past the window.** Beat is restarted in part (b),
the rehearsal Healthchecks check in part (c) is pinged back to green, and the marker
event from part (a) is resolved in Sentry. A drill that ends with a monitor still red is
indistinguishable from an incident.

#### (a) A controlled Sentry event

The question is whether an exception raised on this box reaches the project with enough
context to act on, and without carrying anything it should not.

Raise it with a marker string so the event is unambiguously the drill's and not a real
error that happened to land in the same minute:

```sh
cd /opt/app-mei
compose='docker compose --env-file .env.prod -f docker-compose.prod.yml'
$compose exec -T web python -c \
  'raise RuntimeError("PILOT-504-DRILL-<window-id> deliberate controlled event")'
```

Acceptance, taken from the event's JSON in the Sentry UI and transcribed rather than
screenshotted:

| Field | Expected |
| --- | --- |
| Message | contains `PILOT-504-DRILL-<window-id>` |
| `release` | equals the deployed SHA, `<deployed-sha>` |
| `environment` | equals the configured value, `<sentry-environment>` |
| Credential fields | **absent** — no password, no confirm key, no reset key, no invite token, no `EMAIL_HOST_PASSWORD`, in the event body, the request data, the breadcrumbs, or the stack-frame locals |

`release` is worth asserting rather than assuming. `config/settings/prod.py:313-315` sets
it **only** when `current_release()` returns something other than the unknown sentinel,
so an image built without `--build-arg GIT_SHA` produces events with no release field at
all — and those events cannot be attributed to a deploy, which is most of what makes them
useful. `environment` comes from `SENTRY_ENVIRONMENT` (`config/settings/prod.py:302`) and
defaults to `production`; record which of the two it was.

The scrubbing assertion is the reason this is a drill rather than a code review. The
denylist, the recursive scrubber, and both send hooks are described under [Credential log
hygiene](#credential-log-hygiene) and are exercised by tests, but no event produced by
*this box's* configuration has ever been inspected. Transcribe the event JSON with any
value that would be a secret replaced by its field name and `<redacted>`, and record the
event id as `<sentry-event-id>` in the operator artifact only.

#### (b) The degraded-signal drill: stop beat

This is the part with a counter-intuitive observation in it, and getting that observation
right is the reason the drill exists.

```sh
$compose stop beat
date -u +%Y-%m-%dT%H:%M:%SZ    # record as <beat-stopped-utc>
```

Then wait, and record what happens in this order:

| # | Within | Observation | Command |
| --- | --- | --- | --- |
| 1 | 20 min of `<beat-stopped-utc>` | `/healthz` returns **503** with `"scheduler": "stale"` | `curl -o /dev/null -w '%{http_code}' .../healthz` |
| 2 | — | Compose marks `web` **unhealthy** — and does **not** restart it | `$compose ps` |
| 3 | — | Caddy stays healthy and keeps proxying | `$compose ps`, and a real page fetch |
| 4 | — | A firm page still answers **200** | `curl -o /dev/null -w '%{http_code}' https://<firm-host>/` |
| 5 | after the alert | UptimeRobot **M1** alert delivered to `<alert-recipient>` | the recipient mailbox |
| 6 | 10 min of restart | `/healthz` back to 200, `"scheduler": "alive"`, `web` healthy again | `$compose ps` and the probe |

**Why 20 minutes.** The scheduler stamps its heartbeat every 5 minutes and the probe
calls it stale after 15 (`apps/obligations/heartbeat.py:34-35`), so `/healthz` flips at
most 15 minutes after the last tick. The container healthcheck then polls every 10s and
needs 30 consecutive failures before Compose changes the state
(`docker-compose.prod.yml:270-273`), which is a further 5 minutes. 15 + 5 is the 20.

**Why `web` goes UNHEALTHY, which is REQUIRED EVIDENCE and not a footnote.** The web
container's healthcheck asserts an HTTP **200** — `sys.exit(0 if r.status==200 else 1)`
at `docker-compose.prod.yml:269`. And `/healthz` answers **503** whenever the scheduler
is stale: `status=OK if report.scheduler_alive else SERVICE_UNAVAILABLE` at
`apps/core/views.py:65`. Those two facts compose directly. A dead scheduler therefore
makes the **web** container fail its healthcheck, even though the web process is serving
requests perfectly, and Compose marks it `unhealthy`.

That is by design and both halves of it must be observed:

- **It goes unhealthy.** Do not record this as "web may appear degraded". The container
  is marked unhealthy, deterministically, and an operator reading `docker compose ps`
  during a real stale-scheduler incident will see it. If they do not know why, they will
  chase the web container while the actual fault is in `beat`.
- **It is not restarted.** `restart: unless-stopped` restarts a container whose **process
  exits**; an unhealthy container whose process is alive is left alone. Nothing in this
  stack converts a failing healthcheck into a restart. Assert the container's start time
  is unchanged across the drill, because "it didn't restart" and "it restarted so fast I
  missed it" look identical in `ps`:

  ```sh
  $compose ps --format '{{.Name}} {{.State}} {{.Status}}'
  docker inspect --format '{{.State.StartedAt}} {{.State.Health.Status}}' \
    "$($compose ps -q web)"
  ```

  Acceptance: `Health.Status` is `unhealthy` and `StartedAt` equals the value recorded
  before `beat` was stopped.
- **Caddy keeps proxying.** Its own healthcheck targets Caddy's `:8080` responder rather
  than the proxied site (`docker-compose.prod.yml:383-384`), so it does not inherit web's
  state, and its `depends_on: web: service_healthy` is a **start-time** condition that has
  no effect on a container already running. Traffic is unaffected, which is why row 4 must
  show a real firm page at 200 and not merely a `/healthz` that is expected to be 503.

Recovery:

```sh
$compose start beat
date -u +%Y-%m-%dT%H:%M:%SZ    # record as <beat-started-utc>
```

Acceptance: within 10 minutes `/healthz` is 200 with `"scheduler": "alive"` and `web`
returns to `healthy`. Ten minutes is one heartbeat interval plus the healthcheck's own
recovery poll, with margin; record the observed elapsed value as
`<recovery-elapsed-seconds>` rather than the bound.

Use `start`, not `up -d beat`. `beat` declares `depends_on: web: condition:
service_healthy` (`docker-compose.prod.yml:320-326`), and `up` evaluates that condition
while `start` does not — so `up -d beat` at the one moment `web` is deliberately unhealthy
would block on the very condition the drill created. That is not a bug to fix, it is a
trap to know about, and it is exactly the shape of the recovery an operator would attempt
under pressure.

Row 5 is a delivery assertion and belongs to the recipient, not to the box. Record which
address received it, the delivery time, and the elapsed interval from
`<beat-stopped-utc>`; the runbook's [monitor-to-action
mapping](PILOT-RUNBOOK.md#monitor-to-action) already routes M1's `scheduler` field to the
stale-scheduler entry, and this drill is what makes that row a measured path.

#### (c) A skipped Healthchecks ping

The backup dead-man is the one alert that fires on **silence**, so it is the one that
cannot be tested by doing something. It is tested by not doing something.

**Use a rehearsal check, never the production backup check.** Create a separate check
with the same period and grace as the production one, record its ping URL as
`<rehearsal-hc-url>` in the operator artifact, and drill against that. Skipping the
production check's `/start` ping fakes a backup failure on a host holding real pilot data,
puts a red mark in the surface an operator uses to answer "did last night run", and
teaches exactly the wrong reflex.

Bring the rehearsal check green, then stop pinging it and wait out its period plus grace:

```sh
curl -fsS -m 10 --retry 3 "<rehearsal-hc-url>"          # green
# then send nothing, for period + grace
```

Acceptance: a **MISSED** / "check is down" notification is delivered to
`<alert-recipient>`, and the elapsed interval from the last successful ping is recorded
as `<hc-missed-elapsed>`. Ping the check once more at the end of the window so it returns
to green before the window closes.

#### Evidence

| Artifact | Contents |
| --- | --- |
| `.evidence/PILOT-504-happy.txt` | The part (a) event JSON with secrets replaced by `<redacted>`; the part (b) six-row observation table with UTC timestamps, the `StartedAt`/`Health.Status` pair, and the firm page's 200; the part (c) MISSED notification and its elapsed value |
| `.evidence/PILOT-504-failure.txt` | The controls: `/healthz` observed at 200 with `"scheduler": "alive"` **before** the drill, so the 503 is attributable to the stop; and the rehearsal check observed green before pings ceased |
| `.evidence/PILOT-504-operator.txt` | `<window-id>`, window start and end, `<sentry-event-id>`, `<alert-recipient>`, and PASS/FAIL per part. No ping URLs, no DSN, no credentials |

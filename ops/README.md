# Operations

Most of this file describes mechanisms that exist and have been exercised. The last
section, [Operator runsheets](#operator-runsheets-procedures-and-as-executed-records), is
different: it holds the procedures written ahead of the pilot host migration, and each
one now opens with a dated record of how it actually ran. The migration was executed:
the public name has served from the Lightsail host since 2026-09-22, and the old OCI
compute instance is stopped with its disk intact. The runsheets stay beside the
mechanisms they configure, so a re-run starts from the same text that was executed.

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
| Docker/container log retention | partial | **CONTENT SANITIZED; RETENTION OPERATOR GATE.** The aggregate local container trace had zero canary hits across all four measured paths after the Django and Caddy filters. Neither Compose file contains a `logging:` block, so driver choice and rotation remain host-level state that the operator must configure and verify. Verified on the Lightsail target on 2026-09-21 (`.evidence/PILOT2-209-operator.txt`); the procedure and its record are in [Host log rotation](#host-log-rotation). |
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

- [`Recovery timing status`](RESTORE.md#recovery-timing-status) — the measured
  PILOT2-302/303 recovery times, each with its evidence file.
- [`Secrets and environment`](RESTORE.md#secrets-and-environment) — password-manager
  source location and safe `.env.prod` reconstruction.
- [`Path A — physical restore with WAL replay`](RESTORE.md#path-a--physical-restore-with-wal-replay-rehearsed)
  — the local base-plus-WAL/PITR procedure and mixed-format archive checks.
- [`Path B — off-host logical restore`](RESTORE.md#path-b--off-host-logical-restore-ownership-pinned)
  — the ownership-pinned total-host-loss path.
- [`DNS`](RESTORE.md#dns) — Cloudflare inventory shape, TTL handling, and public checks.
- [`Object storage`](RESTORE.md#object-storage) — restored-row/live-byte Test A and
  `_probe/` version-recovery Test B.

The object-recovery layer those tests depend on is armed. Bucket versioning and the
30-day previous-version prune rule were read back on the documents bucket on
2026-09-21, and a prior version was restored byte for byte; the record is in [Object
storage: versioning, lifecycle, probe](#object-storage-versioning-lifecycle-probe).

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
leave `OFFHOST_S3_BUCKET` absent; its presence is the script's activation switch. Local
MinIO rehearsals are only stand-ins and must never be described as production evidence.

The gate has passed. The copy was armed on the OCI host on 2026-09-21
(`.evidence/PILOT2-301-operator.txt`: versioning and SSE verified, the reader identity
refused listing, and the writer refused deletion), placed on the Lightsail target at
Phase B (`.evidence/PILOT2-402-operator.txt`), and proven from the target by the
scheduled nightly of 2026-09-24, read back by a separate reader credential with a
matching SHA-256 (`.evidence/PILOT2-406-operator.txt`).

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

After any stop longer than 15 minutes, `/healthz` is expected to return 503 until beat's first tick writes a scheduler heartbeat. The web service may report unhealthy for up to about 7 minutes; this is expected and self-heals.

The pilot host deploys itself. A push that lands on `main` and passes both gates ships
to the Lightsail instance with no human in the loop, and the job proves the deployment
landed by observing its **effects** rather than by trusting that the containers came
up. Everything below describes `deploy-pilot` in `.github/workflows/ci.yml`; the
manual path exists for the day GitHub is unavailable, not as the normal route.

### The job

The trigger is `push` on `refs/heads/main`, and both halves of that condition are
load-bearing. `push` excludes `pull_request` runs, whose head is a synthetic merge
commit that exists on no branch and therefore cannot be checked out on the box; the ref
check excludes any branch a future trigger might add. A pull request still runs `test`
and `container-smoke` — it just never reaches the deploy.

Gating is `needs: [test, container-smoke]`, so lint, types, the migration check, the
full suite, the isolation suite and a cold-booted `/healthz` all pass before anything
touches the server.

The job runs against the `lightsail-pilot` GitHub environment, which is what scopes
the OIDC token's `environment` claim and gates access to the `_LIGHTSAIL` secrets —
the IAM trust policy accepts that claim and nothing else. Its permissions are
`id-token: write`, to mint the OIDC token, and `contents: read`; nothing more.

The mechanism is deliberately unglamorous:

1. **Refuse incomplete configuration** — every `_LIGHTSAIL` secret and variable must
   be non-empty before anything runs, so a half-configured environment fails on the
   missing name rather than mid-deploy.
2. **Build both images on the runner** with plain `docker build`, never `docker compose
   -f docker-compose.prod.yml build`. That file carries roughly fifteen mandatory
   `${VAR:?}` interpolations and no `.env.prod` exists on a runner, so compose aborts
   while *parsing*, before it would ever reach a build. The app image takes
   `--build-arg GIT_SHA`; the Caddy image is built from `ops/caddy`.
3. **Open the firewall just in time.** The host's SSH port is closed to the world at
   rest. The job mints short-lived AWS credentials through OIDC
   (`configure-aws-credentials`, role `AWS_ROLE_ARN_LIGHTSAIL`), reads the runner's own
   public address, and opens TCP/22 to exactly that `/32`. Before mutating, it
   snapshots the whole port state and verifies the preservation baseline — the
   operator's `/32`s in `LIGHTSAIL_PRESERVED_SSH_CIDRS`, the `lightsail-connect`
   browser-SSH alias, and ports 80/443 — then verifies afterwards that nothing but the
   runner rule changed. Two `if: always()` steps close the rule and verify the
   baseline is intact, so the host is never left SSH-open to a runner address. A
   runner that dies mid-job leaves its rule behind; `deploy-lightsail.yml`'s
   `stale-rule-cleanup` is the bounded manual path for removing it.
4. **Sync the checkout** with `git fetch --prune origin && git reset --hard "$GIT_SHA"`.
   The box needs the tree as well as the images, because compose reads
   `docker-compose.prod.yml`, `ops/Caddyfile` and `ops/sql` from disk there. **This runs
   before anything lands on the box**, and the ordering is load-bearing — see below.
   Right after it, `ops/check_env_prod.py` audits the box's `.env.prod` for leftover
   placeholders, printing key names only.
5. **Preflight disk and memory** on the box: `MIN_FREE_KIB` free under
   `/var/lib/docker`, plus `MemAvailable` and `SwapFree` floors, because the observed
   failure is a stalled `docker load`, not a clean error.
6. **Anchor the rollback** by tagging the currently running images `:previous` on the
   box — before the load, because once `docker load` overwrites the `:prod` tags the
   previous generation is unreachable by name.
7. **Ship the pair** as `docker save … | gzip -1 | ssh 'set -euo pipefail; gunzip |
   docker load'`. The images are never built on the server; see the next section for
   why.
8. **Roll the stack** with `up -d --no-build --wait`, no `--force-recreate` and no
   service list. Compose already recreates exactly the containers whose image id moved,
   and naming services would silently skip any service added to the file later.
9. **Assert six times, by effect** (below), then run the post-rollout storage probe,
   and only then `docker image prune -f`. Pruning earlier would delete the layers the
   `:previous` tags depend on, destroying the rollback while the deploy was still
   unproven.

#### Sync before the load, because only one of those two is reversible

The Sync and the load do not depend on each other. The Sync touches only git under
`$DEPLOY_PATH_LIGHTSAIL`; the load touches only the docker daemon. Neither reads what
the other writes, so the order is free to choose — and exactly one choice is safe.

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
| 5 | `GET https://samaronefialho.dev/readyz`, resolved onto the deploy target, reports `ready`/`ok`/`ok` | A stack that answers liveness while its database or Redis is not actually ready |
| 6 | `GET https://samaronefialho.dev/versionz`, resolved onto the deploy target, reports `.release == $GIT_SHA` | An edge that answers from some *other* box, or from a stack whose public face is not the commit just shipped |

Assertions 1 and 6 look like the same question and are not. Assertion 1 reads
`/app/RELEASE` through ssh, from inside the container — it proves the box took the
image. Assertion 6 reads `/versionz` from the runner through the public TLS edge, so it
proves the thing the internet reaches serves that commit. A stack can pass 1 and fail 6.

Assertions 4, 5 and 6 pin resolution with
`curl --resolve samaronefialho.dev:443:$LIGHTSAIL_IP`, where `LIGHTSAIL_IP` comes from
the Lightsail control plane (`aws lightsail get-instance`), never from DNS — and the
channel-open step refuses to proceed if `DEPLOY_HOST_LIGHTSAIL` resolves to anything
else. That is what makes them assertions about **the box just deployed** rather than
about whatever DNS currently points at — which matters most during the cutover window,
when the name still resolves to the old OCI box on purpose. A `dig +short` anywhere in
`ci.yml` would be that mistake returning, so `tests/scope/test_deploy_binding.py`
forbids the token outright.

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

The deploy is driven by five environment-scoped secrets on `lightsail-pilot` —
`DEPLOY_HOST_LIGHTSAIL`, `DEPLOY_USER_LIGHTSAIL`, `DEPLOY_PATH_LIGHTSAIL`,
`DEPLOY_SSH_KEY_LIGHTSAIL` and `DEPLOY_KNOWN_HOSTS_LIGHTSAIL` — plus the environment
variables `LIGHTSAIL_INSTANCE_NAME`, `LIGHTSAIL_PRESERVED_SSH_CIDRS` and
`AWS_ROLE_ARN_LIGHTSAIL`. The OCI-era repository secrets `DEPLOY_HOST`, `DEPLOY_USER`,
`DEPLOY_PATH`, `DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS` are retired at cutover: no
workflow references them any longer, and they are deleted rather than left to rot.

Secrets arrive as environment variables rather than `${{ }}` interpolated into the
shell script, because interpolation splices the value into the shell *source*, where a
newline or a quote in a secret becomes executable text. The private key is written to
`$RUNNER_TEMP`, used through a wrapper that sets `IdentitiesOnly=yes` and
`IdentityAgent=none`, and shredded in an `if: always()` step. The AWS credential is
minted per run through OIDC and expires with it; nothing long-lived for AWS exists in
the repository at all.

The blast radius is worth stating without euphemism. The deploy key authenticates as
`ubuntu`, and that account's groups include `docker`. Membership in `docker` is
**root-equivalent**: it permits starting an arbitrary container with an arbitrary host
bind-mount, which is a complete filesystem read/write as root by design of the daemon,
not by a bug. Therefore anyone who can push to `main`, and anyone who extracts
`DEPLOY_SSH_KEY_LIGHTSAIL`, controls the pilot host. That includes the Postgres data
volume and `/opt/app-mei/.env.prod`, which holds every production credential,
including `OCI_S3_SECRET_ACCESS_KEY` and therefore the offsite backups.

This is an accepted trade, approved at the planning gate: a single-maintainer
repository does not carry the operational weight of a hardened deploy account with a
rootless daemon and a restricted `command=` in `authorized_keys`. It is a deliberate
decision with a known cost, recorded here so that the decision is visible when the
cost changes — a second contributor or real customer data both change it.

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
gh secret set DEPLOY_SSH_KEY_LIGHTSAIL -R samaronejr/app-mei \
  --env lightsail-pilot < /tmp/deploy_new

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

The job declares `concurrency: { group: lightsail-pilot-firewall, cancel-in-progress:
false }`. Never cancelling in flight is the important half: a half-loaded image or a
stack caught mid-`up` is a worse state than a queue, and a cancelled job may skip the
`if: always()` firewall cleanup, stranding the runner's SSH rule open. The group is
shared with `stale-rule-cleanup` in `deploy-lightsail.yml` so the deploy and the
cleanup can never mutate the same firewall concurrently.

The consequence is that GitHub retains only the **newest pending** run per group, so
back-to-back pushes supersede one another and the superseded deploy reports `cancelled`
without ever running. That is by design and not a failure. Anything that verifies deploy
history must therefore read the latest **completed** run, not the latest run:

```sh
gh run list -R samaronejr/app-mei --workflow=ci.yml --branch=main --status=completed --limit=1
```

### Manual fallback

For when GitHub Actions is unavailable. This is the CI job by hand, in the same order,
and it must be run from a clean checkout of the commit being deployed. The operator's
own SSH `/32` is already in `LIGHTSAIL_PRESERVED_SSH_CIDRS`, so the manual path needs
no firewall change — the JIT rule exists only for ephemeral runner addresses.

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

Then run all **six** assertions by hand, and the storage probe after them. A manual
deploy that skips them is exactly the T-065 situation described above — and one that
runs only the in-stack checks proves nothing about what the public edge actually
serves.

`lightsail_ip` is the same pin CI uses. Take the deploy target's address from the
Lightsail control plane — never from DNS, which during the cutover window still
resolves to the old box — and send every public assertion to it explicitly, or a stale
DNS record will let some other machine answer for the name and the checks will pass
against the wrong host.

```sh
compose='docker compose --env-file .env.prod -f docker-compose.prod.yml'
lightsail_ip="$(aws lightsail get-instance --instance-name <instance> \
  --query 'instance.publicIpAddress' --output text)"

# 1/6 — the running container is this commit.
ssh app-mei "cd /opt/app-mei && $compose exec -T web cat /app/RELEASE"          # == $sha

# 2/6 — migrations applied, with the positive control.
ssh app-mei "cd /opt/app-mei && $compose exec -T web sh -c \
  'DATABASE_URL=\"\$DATABASE_MIGRATION_URL\" python manage.py showmigrations --plan --skip-checks'" \
  | grep -c '\[X\]'                                                             # >= 1, and no [ ]

# 3/6 — system checks green in the deployed container.
ssh app-mei "cd /opt/app-mei && $compose exec -T web python manage.py check"

# 4/6 — /healthz is ok over the public edge, pinned to the deploy target.
curl -fsS --max-time 30 --resolve "samaronefialho.dev:443:$lightsail_ip" \
  "https://samaronefialho.dev/healthz?cb=$sha" \
  | jq -e '.status == "ok"'

# 5/6 — /readyz reports database and Redis ready, pinned to the deploy target.
curl -fsS --max-time 30 --resolve "samaronefialho.dev:443:$lightsail_ip" \
  "https://samaronefialho.dev/readyz?cb=$sha" \
  | jq -e '.status == "ready" and .database == "ok" and .redis == "ok"'

# 6/6 — the deploy target's public face runs THIS commit.
curl -fsS --resolve "samaronefialho.dev:443:$lightsail_ip" \
  "https://samaronefialho.dev/versionz?cb=$sha" \
  | jq -e --arg GIT_SHA "$sha" '.release == $GIT_SHA'
```

`jq -e` is what makes assertions 4 through 6 bite: it exits nonzero when the predicate
is false, so a 200 carrying the wrong body fails instead of scrolling past. Retry 4/6 a
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
| `git fetch --prune origin` / `reset --hard <sha>` | todo 4 QA, `FIX-04-happy`; CI run 30643246807, Sync step; PILOT-403 shadow deploys |
| `git bundle create` + `git fetch <bundle>` | T-065 manual deploy (21 commits shipped this way) |
| `docker compose … up -d --no-build --wait` | T-065 manual deploy; CI run 30643246807; PILOT-403 shadow deploys |
| `manage.py showmigrations --plan --skip-checks` | CI run 30643246807, assert 2/4 (94 applied, none pending); PILOT-403 shadow deploys, assert 2/6 |
| `manage.py check` | todo 1 QA, `FIX-01-happy`; CI run 30643246807, assert 3/4; PILOT-403 shadow deploys, assert 3/6 |
| `curl … /healthz` | todo 3 QA, `FIX-03-happy`; CI run 30643246807, assert 4/4; PILOT-403 shadow deploys, assert 4/6 |
| `curl --resolve … /readyz` + `jq -e` ready/ok/ok | PILOT-403 shadow deploys, assert 5/6 |
| `curl --resolve … /versionz` + `jq -e '.release == $GIT_SHA'` | PILOT-202 QA (endpoint and assertion shape); PILOT-403 shadow deploys, assert 6/6 |
| `aws lightsail open/close-instance-public-ports` (runner /32 only) | PILOT-403 shadow deploys, Open/Close runner-rule steps |
| `manage.py storage_probe` | PILOT-203 QA (`PILOT-203-happy`, against local storage); the real OCI documents bucket from the OCI serving host (`.evidence/PILOT2-405-operator.txt`); the Lightsail target after its first push deploy (`.evidence/PILOT2-403-happy.txt`) and after the manual deploy of `76df579` (`.evidence/deploy-76df579-operator.txt`) |
| Manual fallback plus all six assertions | The `76df579` deploy on 2026-09-24, run because GitHub Actions credits were unavailable. It was a host-side variant: the checkout was synced and both images were built on the Lightsail host itself instead of being shipped with `docker save`, after tagging `:previous` (`.evidence/deploy-76df579-operator.txt`: six of six asserts, storage probe pass) |
| `docker image ls --filter reference="app-mei*"` | todo 6 QA, `FIX-06-happy` (live, against the box) |
| `docker image prune -f` | CI run 30643246807, Reclaim step |
| `gh secret set` / `gh secret list` | todo 4 QA, `FIX-04-happy`; todo 6 QA, `FIX-06-happy` |
| `gh run list --status=completed` | todo 6 QA, `FIX-06-happy` |

### One orphan image on the box

The retiring OCI box still carries `app-mei-caddy:latest` from before the images were renamed to the
`:prod` tags the compose file now references. Nothing points at it and nothing will; it
costs 154 MB and `docker image prune -f` will not touch it because it is tagged. Untag
it (`docker rmi app-mei-caddy:latest`) or leave it. Recorded so that its presence is not
mistaken for a live tag during a rollback.

## Operator runsheets: procedures and as-executed records

These runsheets were written before the pilot host migration and then executed during
`pilot-readiness-v2`, between 2026-09-21 and 2026-09-25. Each section now opens with a
"Recorded as executed on <date>" block: what ran, what it proved, and the departures
from the text below it that matter for a re-run. The procedure text is kept as the procedure,
because the next host move or a re-run starts from it, and where a run showed the text
to be wrong the text was corrected and the record says so.

The records cite operator artifacts as `.evidence/PILOT2-###-operator.txt` (and a few
older `PILOT-###` files). `.evidence/` is the execution worktree's local evidence root,
ignored by git, so these are names rather than links. The artifacts hold ids,
timestamps and PASS/FAIL lines, never secrets. The evidence tables at the end of each
procedure name the artifacts the procedure was written to produce; the v2 execution
recorded into the `PILOT2-*` files cited in the records instead.

Two conventions govern the procedure text, and they are load-bearing:

- Any value only the operator can supply is written as an angle-bracket
  `<placeholder>`: regions, bucket names, IAM ARNs, instance IPs, DNS record values,
  quotas, expiry dates, and every measured timing. A placeholder that survives into a
  transcript is an unfinished step, not a formatting artifact.
- Any result not yet observed is written `PENDING`. There are no plausible-looking
  sample outputs anywhere in this block, because a plausible sample is exactly what a
  later reader mistakes for evidence.

Where a command's exact form depends on an operator choice, the shape is shown with the
placeholder in place rather than guessed. A provider emits its own DKIM key and its own
bounce-domain MX host, so those appear as `<dkim-public-key-as-generated-by-resend>` and
`<priority-and-host-as-generated-by-resend>`, never as a value invented while writing
documentation.

### Email provider (Resend SMTP)

Covers PILOT-104 (account, sending domain, DKIM, SPF, DMARC alignment, SMTP credential)
and PILOT-105 (the daily cap, bounce and complaint handling).

#### Recorded as executed on 2026-09-21

- **Domain and DNS** (`.evidence/PILOT2-104-operator.txt`). The Resend domain status is
  Verified in region `sa-east-1` with `send` as the return path. Public lookups from a
  resolver that did not create the records show the DKIM TXT on
  `resend._domainkey.samaronefialho.dev` and the MX and SPF on `send.samaronefialho.dev`.
  The apex SPF, apex MX and DMARC records are unchanged.
- **Deviation, record layout.** Resend now generates the DKIM TXT plus two DNS-only
  CNAMEs for the `send` subdomain, not the MX and TXT pair the table below shows. The
  CNAMEs resolve to that MX and SPF, so the table still describes what a resolver sees.
- **Deviation, NXDOMAIN control.** A random name under the apex answers NOERROR with no
  data, because the zone has a wildcard. The control that does answer `NXDOMAIN` is a
  nonexistent child of `resend._domainkey`, which the wildcard cannot synthesize.
- **Deviation, DMARC.** The published record is
  `v=DMARC1; p=none; adkim=r; aspf=r; pct=100`, with no `rua=` tag. Aggregate reports
  aren't being collected yet.
- **Credential smoke from outside the app** (`.evidence/PILOT2-105-operator.txt`). The
  stdlib `smtplib` in a throwaway container stood in for swaks: authentication `235`,
  message accepted `250`, and a wrong password refused with `535`. The received message
  shows SPF, DKIM (`samaronefialho.dev`) and DMARC all `pass`. It landed in the spam
  folder, so authentication is proven and inbox placement is not. The API key is
  restricted to sending on this domain and kept in the password manager. The account
  reports the free tier's 100 per day and 3,000 per month.
- **Send from the target, before the flip** (`.evidence/PILOT2-106-operator.txt`,
  Lightsail at `c7afe36`). `sendtestemail` from the web container exited 0 and arrived
  with SPF, DKIM and DMARC `pass`, again in spam. A wrong password raised
  `SMTPAuthenticationError` and delivered nothing. The receiver's `Received` header
  names Amazon SES's `sa-east-1` outbound relay, not `smtp.resend.com`: Resend relays
  through SES, and no header was invented to match the sheet.
- **Bounces.** A provider bounce-simulator address produced a permanent `550 5.1.1`
  bounce on 2026-09-22, handled by the manual suppression procedure below with no
  repeat sends (`.evidence/PILOT2-106O-operator.txt`).
- **Still open.** The daily cap has not been reached, so `<cap-refusal-response>` in
  [When the daily cap is hit](#when-the-daily-cap-is-hit) remains unobserved.

Resend replaces SES here for one reason: the pilot has to send to arbitrary external
mailboxes from its first day, and SES puts that behind a human-reviewed production-access
request whose answer arrives when it arrives. Resend's free tier sends to any recipient
the moment the domain verifies, and it publishes an SMTP relay, so the application keeps
the provider-neutral `EMAIL_*` settings it already has. Nothing in `config/settings/prod.py`
changes, `core.E013` keeps refusing an incomplete transport exactly as before, and the
swap is a credential swap. The SES runsheet is kept verbatim at the end of this section as history. It is not a
procedure to run.

#### The free tier, and where it stops

| Limit | Free tier |
| --- | --- |
| Messages per day | 100 |
| Messages per calendar month | 3,000 |
| Verified domains | 1 |
| SMTP relay | included |

Source: <https://resend.com/docs/knowledge-base/account-quotas-and-limits>, read while
this runsheet was written. Re-read it at execution time and transcribe what the account
itself reports into `.evidence/PILOT-104-operator.txt`. A published limit and an account's
limit are two different claims, and only the second one bills.

One domain is the constraint that bites first. `samaronefialho.dev` is that domain, which
means there is no separately verified staging sender; anything sent while rehearsing comes
out of the same 100 a day the pilot needs. The cap itself is handled under [when the daily
cap is hit](#when-the-daily-cap-is-hit).

#### Account and sending domain

1. Create the Resend account on `<operator-mailbox>` and turn on MFA before anything else
   goes in it. The API key that this account can mint is a sending credential for the
   pilot's only domain.
2. Add `samaronefialho.dev` as a domain.
3. Keep Resend's default sending subdomain, `send.samaronefialho.dev`. That subdomain
   carries the envelope sender, so bounce traffic and SPF authentication both land there
   instead of on the apex. Leaving the apex alone is not a preference here, it is what
   keeps Cloudflare Email Routing working.
4. Resend then generates the verification records. Copy them out of the dashboard exactly
   as shown; record their **names and types** in the operator artifact and leave the values
   where they belong, in DNS.

#### The records Resend generates, placed exactly as generated

| Name | Type | Value |
| --- | --- | --- |
| `resend._domainkey.samaronefialho.dev` | TXT | `<dkim-public-key-as-generated-by-resend>` |
| `send.samaronefialho.dev` | TXT | `<spf-value-as-generated-by-resend>` |
| `send.samaronefialho.dev` | MX | `<priority-and-host-as-generated-by-resend>` |

Three rules govern placing them:

- **Copy, never retype.** A DKIM public key is a few hundred characters of base64 and a
  single transposed character produces a record that exists, resolves, and fails to
  verify. That failure mode reads as "DNS not propagated yet" for as long as you let it.
- **DNS-only, grey cloud, all three.** A proxied record resolves to Cloudflare's edge, and
  verification then runs against something that is present but not what Resend wrote.
- **The selector is fixed.** Resend signs with the `resend` selector, one key per domain,
  so there is no second selector to rotate through. Rotating the DKIM key means
  re-verifying the domain, which is a planned maintenance step, not a quick fix during an
  incident.

#### What must not change at the apex

Two apex records already exist and stay untouched:

```text
samaronefialho.dev  TXT  "v=spf1 include:_spf.mx.cloudflare.net ~all"
samaronefialho.dev  MX   <cloudflare-email-routing-hosts-unchanged>
```

The apex SPF authorises Cloudflare, which is what handles mail for the domain. Resend's
envelope sender lives on `send.samaronefialho.dev`, so a receiver checking SPF for Resend
mail never reads the apex record at all. Adding an `include:` for the new provider to the
apex would buy nothing, spend one of SPF's ten DNS lookups, and invite someone later to
"clean up" the Cloudflare include. Publishing a second apex SPF TXT is worse: two SPF
records on one name is a permanent error, and the result is that *every* SPF check on the
domain fails, inbound routing included.

The apex MX is Cloudflare Email Routing and is what makes addresses at the domain
receivable. The new MX sits on `send.samaronefialho.dev`, a different name, so the two
never compete.

If any step, here or in a provider's setup wizard, asks you to edit the apex SPF or the
apex MX, that step is wrong for this domain. Stop and record why.

#### DMARC, and why relaxed alignment is enough

```text
_dmarc.samaronefialho.dev  TXT  "v=DMARC1; p=none; adkim=r; aspf=r; rua=mailto:<operator-mailbox>"
```

The alignment argument, in full, because this is the part that silently breaks when a
provider changes:

- The visible `From` is `nao-responda@samaronefialho.dev`, the apex, because
  `DEFAULT_FROM_EMAIL` says so and all three send sites pass `from_email=None`.
- The DKIM signature carries `d=samaronefialho.dev`: the selector record is
  `resend._domainkey.samaronefialho.dev`, so the signing domain is the apex itself. DKIM
  alignment is therefore exact, and would hold even under `adkim=s`.
- SPF authenticates the **envelope** domain, `send.samaronefialho.dev`. That is a
  subdomain of the `From` domain, not the same name, so strict SPF alignment would fail
  and relaxed passes on the organisational-domain match. This is precisely why `aspf=r`
  is written out rather than left to the default.

DMARC passes when either mechanism aligns. Here both do under relaxed, and DKIM survives
forwarding while SPF does not, so the pair is deliberate rather than redundant.

`p=none` is the pilot's policy on purpose: it reports without quarantining, so a
misconfiguration arrives as an aggregate report instead of as invitations that vanish.
Tightening it needs report data that does not exist yet.

#### Verify by effect, with a negative control

Run the lookups **before** placing anything, to capture the red state, then again after.
The last two lines are regression checks on records this work must not touch:

```sh
dig +short TXT resend._domainkey.samaronefialho.dev
dig +short TXT send.samaronefialho.dev
dig +short MX  send.samaronefialho.dev
dig +short TXT _dmarc.samaronefialho.dev
dig +short TXT samaronefialho.dev   # apex SPF: must still be the Cloudflare include
dig +short MX  samaronefialho.dev   # apex MX: must still be Cloudflare Email Routing
```

Then one deliberately absent name, which must answer `NXDOMAIN`. Without it, a resolver
that wildcards the zone makes every lookup above look successful:

```sh
dig +noall +comment <deliberately-absent-name>.samaronefialho.dev
```

Acceptance has two halves and needs both: the records resolve publicly from a machine
that did not create them, and the Resend dashboard reports the domain verified. A
dashboard that says verified while public DNS has not caught up is a race, not a result.

#### The values that go into `.env.prod`, by name

| Variable | Value |
| --- | --- |
| `EMAIL_HOST` | `smtp.resend.com` |
| `EMAIL_PORT` | `587` |
| `EMAIL_USE_TLS` | `true`, with `EMAIL_USE_SSL=false` (the shipped example writes `True`/`False`; either spelling parses) |
| `EMAIL_HOST_USER` | `resend` |
| `EMAIL_HOST_PASSWORD` | the Resend API key, scoped to sending on `samaronefialho.dev` |
| `DEFAULT_FROM_EMAIL` | `nao-responda@samaronefialho.dev` |

`EMAIL_HOST_USER` is the literal string `resend`, the same for every account. It reads
like a placeholder somebody forgot to fill in, and it isn't one.

`EMAIL_USE_TLS` and `EMAIL_USE_SSL` must disagree: `core.E013` refuses a configuration
where both are true or both are false, so a copy-paste that sets SSL without clearing TLS
fails at startup rather than at send time.

The API key is created with sending permission for this domain only. A full-access key in
`.env.prod` is readable by anything that can read the file and can also delete the domain
it sends from. Record the key's **name** and creation date in the operator artifact and
nothing else: no key material in the repository, in evidence files, or in a ticket.

Rotation keeps the two-key discipline, because delete-then-create has a window in which
the stack cannot send:

1. Create a second API key with the same scope. Both are valid.
2. Replace `EMAIL_HOST_PASSWORD` in `.env.prod` on the host and recreate the services that
   read it: `web`, `worker`, `beat`.
3. Prove the new key sends, with the smoke test below, to a mailbox the operator holds.
4. Only then revoke the old key. Revoking before step 3 turns maintenance into an outage.

#### The smoke test: swaks first, then Django

Two probes, in this order, because they fail differently and the order is what localises
the fault.

```sh
swaks --server smtp.resend.com:587 --tls \
      --auth-user 'resend' --auth-password '<resend-api-key>' \
      --from 'nao-responda@samaronefialho.dev' --to '<operator-mailbox>'
```

That exercises the credential, STARTTLS, and Resend's acceptance with none of the
application in the path.

```sh
docker compose -f docker-compose.prod.yml exec web \
  python manage.py sendtestemail <operator-mailbox>
```

That exercises what the container actually has: the `EMAIL_*` values in `.env.prod`, the
TLS flags, `EMAIL_TIMEOUT`, and `DEFAULT_FROM_EMAIL` as the sender every send site
inherits.

Acceptance: the message arrives, and its headers show `DKIM=pass` with
`d=samaronefialho.dev`, `SPF=pass` for `send.samaronefialho.dev`, and `DMARC=pass`. Paste
the authentication-results header block into `.evidence/PILOT-104-happy.txt`. The API key
never goes there.

When swaks delivers and `sendtestemail` does not, the fault is in `.env.prod` or in the
container that read it, not at the provider. The other way round means the credential or
the domain, and re-running the app probe will not tell you which.

#### Bounces and complaints: a dashboard and a human, no webhook

This stack has no webhook receiver for delivery events and none is being added for the
pilot. A receiver is application code, a new unauthenticated public route, and a signature
check that has to be right the first time, all to automate a feed that will carry a
handful of events a week at 100 messages a day. The dashboard plus a person is the honest
trade at this volume, and it is written down here so nobody later reads the absence as an
oversight.

The procedure, then:

- The operator reads the Resend dashboard's delivery log after every pilot journey and at
  `<review-cadence>` otherwise. Bounced and complained addresses get written into
  `.evidence/PILOT-105-operator.txt` with the date, the recipient, and the reason class.
- Suppression is **manual**. An address that hard-bounced is not sent to again until the
  operator has confirmed it out of band, through a channel that is not that address.
  Re-sending to an address the receiving provider rejected re-earns the bounce and spends
  domain reputation that a new sending domain does not have to spare.
- Silent non-delivery is the symptom to know: the application reports success because the
  provider accepted the message, and the recipient never sees it. Check that recipient in
  the delivery log **before** declaring a PILOT-106 journey failed, so a suppressed
  address is not misdiagnosed as a broken journey.

Compared with the SES design this replaces, there is no SNS topic and no confirmed
subscription pushing bounces at a mailbox nobody has to remember to open. That is a real
downgrade, it is accepted for the pilot at this volume, and it is revisited if sending
volume grows past what one person can read.

**The anonymous DSR path is a monitored sender.** `POST /lgpd/dsr-submit` is reachable
without authentication and its success path notifies the encarregado. An attacker, or an
ordinary typo, can drive outbound mail from an unauthenticated endpoint, and a run of
bounces originating there is the earliest signal that it is being abused. Whoever reads
the delivery log must know this sender exists and what a burst from it means.
`lgpd_renotify` retries the same notification for rows whose send failed, so a
persistently bouncing encarregado address surfaces there too. See [Credential log
hygiene](#credential-log-hygiene) for what must never appear in the logs those bursts
generate.

#### When the daily cap is hit

100 messages a day is a hard ceiling, not a throttle with a queue behind it. Message 101
is expected to be refused in-band, inside the SMTP transaction, rather than accepted and
dropped, and that is the property the behaviour below rests on: the application learns
about the cap at send time, on the send that hit it. Confirm it when the cap is first
reached, and record what actually happened; an accepted-then-dropped message would be a
much worse failure mode and would change this entry.

Given an in-band refusal, what follows is unchanged by the provider swap:

- **Invitations.** `send_mail` raises, `_mark_invitation_delivery_failure` in
  `apps/accounts/views.py` calls `transaction.set_rollback(True)`, captures to Sentry, and
  puts an error in front of the user. No invitation row survives and nobody is told a
  message was sent.
- **LGPD notifications.** `apps/lgpd/views.py` records `notification_last_error` and
  leaves `encarregado_notified_at` NULL, so the row stays pending and `lgpd_renotify`
  picks it up once sending works again.

Those are the truthful-failure semantics PILOT-102 and PILOT-103 established, and the cap
is the cheapest way the pilot will ever get to exercise them against a real provider.
Record the refusal's SMTP response code and text as `<cap-refusal-response>` the first
time it is observed; until then it is PENDING, and no code is guessed here. Do not assume
the class: whether the provider answers 4xx or 5xx decides whether anything upstream would
retry, and that is a measurement, not a preference.

Capacity planning is short. The PILOT-106 journeys are a couple of dozen messages, and
password resets and invitations during a pilot are tens per week. What eats 100 in a day
is a loop or an abused DSR endpoint, so a burned cap is an incident signal first and a
capacity signal second.

#### Evidence

| Artifact | Contents |
| --- | --- |
| `.evidence/PILOT-104-red.txt` | The six `dig` outputs before any record is placed, and the domain's unverified status |
| `.evidence/PILOT-104-failure.txt` | The `NXDOMAIN` control, and a swaks run with a deliberately wrong API key refused at authentication |
| `.evidence/PILOT-104-happy.txt` | The same six `dig` outputs after placement, the verified status, and the delivered smoke test's authentication-results headers |
| `.evidence/PILOT-104-operator.txt` | Account mailbox, API key **name** and creation date, the quota figures the dashboard reports, PASS/FAIL per step. No key material |
| `.evidence/PILOT-105-operator.txt` | The delivery-log review cadence, and every bounce or complaint with date, recipient, and reason class |
| `.evidence/PILOT-105-failure.txt` | The cap refusal when first observed, with its SMTP response code, and the application-side behaviour it produced |

#### Historical: the SES runsheet this replaced

Everything from here to the end of this section is **history, not procedure.** It is the
SES runsheet as it stood before the provider decision changed, kept because the reasoning
about DKIM, custom MAIL FROM, alignment, and two-key rotation is why the Resend section
above is shaped the way it is, and because a deleted runsheet looks like a decision nobody
made. Do not execute any command below. The artifact names it references are reused by the
Resend runsheet above, which is the live one.

As written then, the SES runsheet said: it covers PILOT-104 (region, domain identity,
DKIM, custom MAIL FROM, DMARC, SMTP identity) and PILOT-105 (production access, sandbox
exit, bounce and complaint feedback). **Not executed.** No AWS account, identity, IAM
user, SNS topic, or DNS record described there was ever created, and none exists now.

##### Region choice

The region is recorded once and then propagates into the MAIL FROM MX record, the SMTP
endpoint, and every `aws sesv2` invocation below, so choosing it late means redoing DNS.
Pick for latency to Brazil and for SES availability, record it in the PILOT-104 operator
artifact as `<region>`, and use that same string everywhere. Nothing in the repository
pins a region and nothing should.

##### Domain identity and Easy-DKIM

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

##### Custom MAIL FROM and SPF

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

##### DMARC

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

##### A sending identity that can only send

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

##### Rotation procedure

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

##### Production access and sandbox exit (PILOT-105)

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

##### Bounce and complaint feedback reaches a human

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

##### Suppression list

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

Covers PILOT-106.

#### Recorded as executed on 2026-09-22

Run on the live Lightsail target at `aff1464`, inside an owner-authorized window with no
pilot users, against a synthetic rehearsal tenant (`.evidence/PILOT2-106O-operator.txt`).
The two mailboxes were distinct external inboxes, and the encarregado address was a real
external mailbox, not localhost.

| # | Journey as run | Result |
| --- | --- | --- |
| 1 | Firm invitation, TOTP enrolment, fresh login with MFA | PASS, delivered in 1 s |
| 2 | Portal invitation, TOTP enrolment, fresh login with MFA | PASS, delivered in 2 s |
| 3 | Address verification, confirmed by POST | PASS on the second attempt, delivered in 1 s |
| 4 | Password reset, new password then MFA | PASS, delivered in 1 s |
| 5 | LGPD DSR notification, `encarregado_notified_at` stamped | PASS, delivered in 1 s |
| 6 | Revoke an invitation, reissue it; the old link answers 410 | PASS, delivered in under 1 s |

Every delivered message showed SPF, DKIM and DMARC `pass`. The run also covered a
wrong-password login (no mail sent over a 728 s window), the bounce control, and the
hygiene sweep: seven 12-character fragments, one per credential link class, searched in
Docker logs, Caddy logs and Sentry, with zero hits everywhere and a nonzero positive
control in each place.

Deviations, each recorded in the artifact:

- **Journey order.** The run followed the revised six-journey sheet above, not the
  matrix below. TOTP enrolment was folded into journeys 1 and 2, and the revoke and
  reissue journey was added.
- **Journey 3's first attempt failed.** Redeeming an invitation already marks the
  invited address verified (`apps/accounts/invites.py`), so the confirmation link for
  that address was invalid. The pass used an owner-approved secondary alias of mailbox
  A, with MFA disabled for the change and re-enrolled immediately after.
- **Check 7 also ran against the live stack.** The scratch-shell form below ran on
  2026-09-21 (`.evidence/PILOT2-106-operator.txt`). On 2026-09-22 the owner also
  authorized breaking `EMAIL_HOST_PASSWORD` on the running stack for 55 s under a
  host-local restore timer, to observe the application's side: a truthful UI error, zero
  invitation rows created, and an `SMTPAuthenticationError` event in Sentry. The
  original line was verified restored. Don't repeat that form once pilot users exist.

This section depends on [Email provider (Resend
SMTP)](#email-provider-resend-smtp) having landed first: the domain verified, the SMTP
credential in `.env.prod`, and the smoke test delivered. Until then a failed journey says
nothing about the application, only that the sender is not configured yet.

#### Two mailboxes, and why they are different people

The journeys split across two real, externally hosted mailboxes:

| Name | Role | Used by |
| --- | --- | --- |
| `<mailbox-a>` | the firm-side user — an accountant at the pilot firm | checks 1, 2, 3, 5 |
| `<mailbox-b>` | the portal-side user — a client of that firm | check 4 |

They must be genuinely separate addresses at a provider the operator does not run, not
two aliases folding into one inbox. An alias makes a cross-audience mis-send invisible:
an invitation addressed to the portal user but rendered with the firm user's link would
land in the same place and read as success. Neither may be an address the operator can
reach only through the pilot's own infrastructure: a mailbox that depends on the same
Cloudflare routing the domain uses would keep working through a failure these journeys
exist to catch.

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

The point of the negative control is that checks 1 through 6 succeeded because Resend
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
delivery-latency figure the pilot will have, and because a message that the provider
accepted and that arrived forty minutes later is a different operational fact from one that arrived
immediately.

#### Evidence

| Artifact | Contents |
| --- | --- |
| `.evidence/PILOT-106-red.txt` | The pre-production-access refusal, cross-referenced to `.evidence/PILOT-105-red.txt` rather than re-run |
| `.evidence/PILOT-106-happy.txt` | Checks 1 through 6 and 8: per-journey rows, the window bounds, the line count of the captured window, the positive-control count, and the three fragment counts |
| `.evidence/PILOT-106-failure.txt` | Check 7, the SMTP authentication refusal |
| `.evidence/PILOT-106-operator.txt` | Message-ids, mailbox addresses, timestamps, and PASS/FAIL per check. No tokens, no keys, no passwords |

### Object storage: versioning, lifecycle, probe

Covers PILOT-204.

#### Recorded as executed on 2026-09-21

- **Provider ruling: keep OCI.** The OCI account continues on the always-free tier after
  the trial, the documents bucket shows no reclamation notice, and the free storage
  entitlement persists (`.evidence/PILOT2-000-operator.txt`). The documents stay in the
  OCI bucket. The conditional move to another provider was skipped
  (`.evidence/PILOT2-405M-operator.txt`), so **no storage variable was renamed**: the
  target's `.env.prod` still uses the `OCI_S3_*` names from `.env.prod.example`.
- **Versioning and lifecycle** (`.evidence/PILOT2-405-operator.txt`,
  `.evidence/PILOT2-000-operator.txt`). Versioning is `Enabled`. The lifecycle rule
  deletes previous versions after 30 days, is enabled, and expires no current object. No
  retention rule exists.
- **Deviation, lifecycle readback.** OCI's S3-compatible API answers
  `get-bucket-lifecycle-configuration` with 404 `NoSuchLifecycleConfiguration` even with
  the rule in place, because OCI keeps lifecycle policy in its native API. The rule was
  read back in the native OCI console instead.
- **The probe against the real bucket**, run from the OCI serving host: write,
  authenticated read, anonymous direct refusal, storage-URL anonymous refusal, and
  delete-and-confirm-gone all PASS. The wrong-credential control exited 1, failing at
  the write step, so it had nothing to leave behind.
- **The versioning drill.** Two version ids were seen. After a delete, `head` returned
  404, and the restored bytes' SHA-256 equalled the original's.
- **Deviation, restore method.** `CopyObject` from a prior version returned HTTP 400
  `InvalidArgument` on OCI. The restore that worked is the one below: download the
  exact prior version, then put it back as current.
- **From the target.** The same probe passed on Lightsail after its first push deploy
  (`.evidence/PILOT2-403-happy.txt`) and after the manual `76df579` deploy
  (`.evidence/deploy-76df579-operator.txt`). Restored-row hashes
  were matched against live bucket bytes for two tenants with a report-only reconcile
  on 2026-09-22 (`.evidence/PILOT2-303-operator.txt`).

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

### External uptime monitoring: four keyword monitors and a negative control

Covers PILOT-207's external half.

#### Recorded as executed on 2026-09-22

The monitor inventory was read from the authenticated UptimeRobot console after the flip
(`.evidence/PILOT2-406-operator.txt`): M1a to M1d UP against the target and the M1n
negative control DOWN, as required. Three more keyword monitors were added the same
day, all alert-on-absence, case-sensitive, on a five-minute interval, and mailing the
operator contact:

| Monitor | URL | Keyword |
| --- | --- | --- |
| M2 | `https://samaronefialho.dev/readyz` | `"database": "ok", "redis": "ok"` |
| M3 | `https://samaronefialho.dev/versionz` | `"release": "<deployed-sha>"`, the full 40-character SHA |
| M4 | the portal host's `/healthz` | all four healthy fields as one literal string |

Each of M2 to M4 matches one contiguous literal, so a change in field order or JSON
formatting also alerts. Two owner-approved deviations come with them:

- **M3 pins a literal SHA**, so every legitimate release also alerts until the keyword
  is updated. After each deploy, edit M3's keyword to the new `/versionz` value. It was
  refreshed to `76df579` and then to `699f764` on 2026-09-24, and read back UP both
  times.
- **M4 watches a placeholder portal host** until the real pilot tenant exists. Repoint
  it at that tenant's portal host when the tenant is created.

The beat drills of 2026-09-23 and 2026-09-24 are what proved alert delivery; their
record is under [Alerting drills](#alerting-drills-sentry-the-degraded-signal-and-the-missed-ping).
The first of them also corrected check 5 below.

**UptimeRobot cannot evaluate JSONPath.** The operator's capability proof established
that its only body-inspection primitive is a plain **substring** match over the raw
response. Every earlier note in this repository that describes M1 as one monitor with
"JSON assertions" predates that proof and describes a capability the product does not
have. The semantic, per-field assertions still exist — they moved to the scheduled
[`verify-live`](../.github/workflows/verify-live.yml) workflow, which does have `jq` —
and the external monitor falls back to keyword matching, which is what this section
configures.

#### One monitor per field, because a substring match cannot say which field moved

`/healthz` reports four fields, and they fail for four unrelated reasons: a dead beat, a
nightly backup that stopped, a filling disk, and the aggregate the first of those drives.
A single monitor keyed on one keyword would alert on one of them and stay silent on the
rest. So there are four, each alerting on the **absence** of its own field's healthy
value, plus a fifth that exists only to prove the mechanism can fail at all.

| Monitor | Type | Keyword | Alert when | Owning runbook entry |
| --- | --- | --- | --- | --- |
| M1a | Keyword | `"status": "ok"` | keyword **not** found | whichever of M1b–M1d is also red |
| M1b | Keyword | `"scheduler": "alive"` | keyword **not** found | [stale-scheduler](PILOT-RUNBOOK.md#stale-scheduler) |
| M1c | Keyword | `"backup": "fresh"` | keyword **not** found | [stale-backup](PILOT-RUNBOOK.md#stale-backup) |
| M1d | Keyword | `"disk": "ok"` | keyword **not** found | [low-disk](PILOT-RUNBOOK.md#low-disk) |
| M1n | Keyword | `"scheduler": "impossible-keyword-negative-control"` | keyword **not** found | none — see below |

All five point at the same URL, `https://samaronefialho.dev/healthz`, on the same
interval. M1a is not redundant with the other three: `status` is the only field that also
carries the HTTP status code, so it is the one an operator can correlate with a 503 and
with the container healthcheck.

**Do not configure these as HTTP(s) monitors.** A plain HTTP monitor keys on the status
code, and three of the four conditions above — stale backup, low disk, and an unreadable
heartbeat table — are reported in the body at HTTP **200** by deliberate design (see
[A failed backup is visible at `/healthz`, and does not
503`](#a-failed-backup-is-visible-at-healthz-and-does-not-503) and [The disk fuse](#the-disk-fuse-healthz-reports-headroom-and-does-not-503)).
An HTTP monitor would report those three as permanently UP.

#### The keyword is the exact rendered substring, spaces included

This is the one detail that silently destroys the whole arrangement, so it is stated
before the values rather than after them.

`/healthz` is rendered by Django's `JsonResponse`, which serializes with `json.dumps`
default separators — `", "` between pairs and **`": "` between a key and its value**.
The body carries a space after every colon:

```json
{"status": "ok", "scheduler": "alive", "backup": "fresh", "disk": "ok"}
```

A keyword typed the way it is usually written in code, `"backup":"fresh"` with no space,
matches **nothing**. Configured as alert-on-absence it then reads permanently DOWN, which
at least announces itself. Configured the other way round it reads permanently UP — a
green monitor asserting nothing at all, indistinguishable from a working one until the
night it is needed. Copy the values, do not retype them:

<!-- healthz-keyword-monitors -->

```text
"status": "ok"
"scheduler": "alive"
"backup": "fresh"
"disk": "ok"
```

Those four strings are pinned by `tests/test_healthz_keyword_monitors.py`, which reads
them **out of this file** and asserts each one is a literal substring of a real rendered
healthy `/healthz` response. A space deleted from the block above turns CI red, and so
does a change to the endpoint that stops rendering any of them. The test also asserts the
unspaced form is absent, so the trap described in the previous paragraph is measured
rather than warned about.

#### The negative control: an impossible keyword, permanently DOWN

Four green monitors prove nothing on their own. They look identical whether the keyword is
being matched against a healthy body or is not being evaluated at all — a monitor pointed
at the wrong URL, a paused check, and a free-plan account that silently stopped inspecting
bodies all produce exactly the same four green rows.

M1n is what separates those cases. It uses the same URL, the same interval and the same
alert-on-absence setting as the other four, with a keyword the endpoint can never render:

<!-- healthz-keyword-negative-control -->

```text
"scheduler": "impossible-keyword-negative-control"
```

`scheduler` renders only `alive`, `stale` or `unknown` (`apps/obligations/heartbeat.py`
and `apps/core/views.py`), so no state of the application — healthy, degraded, or with the
heartbeat table unreadable — can produce that string. The same test module asserts its
absence from all three of those rendered bodies.

**Acceptance is therefore four UP and one DOWN, and the DOWN one is required.** M1n
reporting UP means keyword evaluation is not happening, and the other four are worthless
until that is explained. Suppress M1n's notifications rather than deleting it: it is read,
not delivered, and a permanently-alerting monitor that pages nobody is the point.

#### Acceptance

| # | Check | Acceptance |
| --- | --- | --- |
| 1 | M1a–M1d against a healthy deployment | all four UP |
| 2 | M1n against the same deployment | DOWN, with notifications suppressed |
| 3 | M1b during the [beat-stopped drill](#b-the-degraded-signal-drill-stop-beat) | transitions to DOWN, alert delivered to `<alert-recipient>` |
| 4 | M1a and **M1d** during the same drill | both transition to DOWN, at the same 15-minute mark as M1b |
| 5 | M1c during the same drill | goes DOWN on the HTTP 503, although its keyword is still in the body (see below) |

Check 3 is the only one that proves delivery, and it is already scheduled: it is row 5 of
the [beat-stopped drill](#b-the-degraded-signal-drill-stop-beat), which is where the
elapsed interval from `<beat-stopped-utc>` is recorded. Checks 4 and 5 are read from the
same window at no extra cost.

**Four monitors go red from one cause, and M1d is the one that surprises people.** A
stopped beat flips `status` to `degraded`, so M1a follows M1b immediately — that part is
obvious. `disk` is the one that is not: the reading is taken and stamped by the scheduler,
so a stalled writer can no longer vouch for the number it last wrote, and `_headroom`
degrades `ok` to `unknown` on the scheduler's own window rather than reporting a
comfortable value nobody is refreshing (see [The disk
fuse](#the-disk-fuse-healthz-reports-headroom-and-does-not-503)). `DISK_STALE_AFTER`
equals `HEARTBEAT_STALE_AFTER`, so M1b and M1d flip together at fifteen minutes.

M1c's keyword survives: the body still says `"backup": "fresh"`, because the nightly
marker is written by a host systemd timer that a stopped beat container does not touch,
and `tests/test_healthz_keyword_monitors.py` asserts that about the body. The monitor
goes DOWN anyway. A stale scheduler makes `/healthz` answer 503, and UptimeRobot counts
the 503 as down before it looks for any keyword. The text here used to say M1c stays UP;
the drill of 2026-09-23 showed otherwise (`.evidence/PILOT2-504-aff1464-operator.txt`,
incident reason `503 Service Unavailable`), and the owner amended the acceptance before
the re-run. **Read four simultaneous reds as one fault, not four.** Chasing M1d as a
capacity incident during a scheduler outage is the specific mistake this note exists to
prevent, and it is the same class of confusion as the web container going `unhealthy`
while web is serving perfectly. A stale backup on its own still leaves `/healthz` at 200,
so M1c alone going red keeps its meaning.

Record monitor ids and the alert recipient in `.evidence/PILOT-207-operator.txt`; the UP
and DOWN states go to `.evidence/PILOT-207-happy.txt`, and check 2's required DOWN plus
its keyword go to `.evidence/PILOT-207-failure.txt`. Never record an API key or a ping
URL in any of them.

### Host log rotation

Covers PILOT-209.

#### Recorded as executed on 2026-09-21

On the Lightsail target, before the flip (`.evidence/PILOT2-209-operator.txt`). Phase A
had already written `daemon.json` with `json-file`, `50m` and `5` on 2026-08-16
(`.evidence/PILOT-402-happy.txt`), and Phase B re-read it
(`.evidence/PILOT2-402-operator.txt`).

- **Every application container** (six checked) reports `max-size=50m` and
  `max-file=5`.
- **The flood test** pushed 300 MiB through a throwaway container that inherited the
  daemon defaults. Rotation left five files, the largest 50,000,134 bytes, with
  210,671,760 bytes retained in total. The container was removed afterwards.
- **journald** uses 71 MiB against `SystemMaxUse=500M`. Deviation: the effective cap comes
  from the drop-in `/etc/systemd/journald.conf.d/app-mei.conf`, so the `grep` of the main
  `journald.conf` below finds nothing. Read the drop-in as well.
- **The invalid-config control** was rejected with exit 1 naming the unknown key. The
  live daemon config was unchanged and no production service restarted.
- **The canary check.** A marker sent as a user agent was found once (the positive
  control) and an absent marker zero times. The credential sweep used the exact values
  of the ten configured secrets, held in memory only, across current and rotated logs
  of all six containers: zero matches. The token-fragment sweep from the mailbox
  journeys ran on 2026-09-22 with zero hits (`.evidence/PILOT2-106O-operator.txt`).

Rotation was never verified on the OCI host. It's stopped now, so its log retention no
longer bears on the pilot.

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

Covers PILOT-401.

#### Recorded as executed on 2026-08-11

The operator chose AWS Lightsail in `sa-east-1`. Measured over SSH rather than taken
from the plan page (`.evidence/PILOT-401-operator.txt`): Ubuntu 24.04.4 LTS, 2 vCPU,
3.7 GiB of usable RAM, 77 GB of disk, systemd running, static IP attached. Every row
below was met except swap. The image ships with none, which the procedure counts as
UNMET, so the bootstrap stayed blocked until Phase A added swap. The host reported
2,047 MiB of swap before Phase B and 2,111 MiB after it (`.evidence/PILOT2-402-operator.txt`).
The instance firewall allows 80 and 443 from anywhere and SSH only from the operator's
address and Lightsail's browser SSH (`.evidence/PILOT2-000-operator.txt`); the deploy
job opens SSH to its runner just in time.

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
| Region | acceptable latency to Brazil | same | Users and the host region should not disagree by an ocean |
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

Covers PILOT-402.

#### Recorded as executed on 2026-08-16 (Phase A) and 2026-09-21 (Phase B)

**Phase A** ran on the Lightsail host on 2026-08-16 (`.evidence/PILOT-402-happy.txt`,
`.evidence/PILOT-402-operator.txt`). Docker and the compose plugin came from Docker's
official repository. The deploy user ran `hello-world`, and a read-only repository deploy
key gave the checkout a noninteractive fetch. `daemon.json` was validated, with its
invalid-key control rejected, and a throwaway flood stayed inside 50m x 5. The units
were copied and validated but never enabled, `install.sh` was not run, and no
application container or volume existed afterwards.

**Phase B** ran on 2026-09-21, from 19:55 to 20:13 UTC
(`.evidence/PILOT2-402-operator.txt`):

- The checkout fast-forwarded cleanly to `origin/main` at `16bce80`. The units were
  re-copied, `systemd-analyze verify` passed, and the timer stayed `disabled` and
  `inactive` with an empty service journal.
- `ops/check_env_prod.py` printed OK for 35 keys. Run against the example file itself it
  failed, as designed. `docker compose config` passed.
- Deviation, failure path: rather than omit a variable, the run overrode `SECRET_KEY`
  to empty for one process with the real env file, and compose refused with exit 1.
- `.env.prod` is owned by the deploy user, mode 0600. `/etc/app-mei/backup.env` holds
  eight keys, `root:root` mode 0600. The off-host AWS CLI config is in place.
- The existing application credentials were carried over from the OCI host. Resend
  and Sentry came from the password manager, and both Healthchecks checks are new and
  scoped to this host. That carry-over is why
  [decommissioning](#decommissioning-the-old-host) rotates the OCI-era secrets.
- Log rotation, the journald cap, swap and AWS CLI v2 were re-verified. The stack was
  not started, and the temporary SSH rule was removed afterwards.

**Correction found after the flip.** The first scheduled nightly on the target, on
2026-09-23, took the local backup and then failed the off-host copy with exit 5
(`.evidence/PILOT2-406-operator.txt`). The backup service runs as the deploy user, and
the root-owned `/etc/app-mei` at mode 0700 kept that user from reading the non-secret
AWS CLI config. The fix was `root:<deploy-user>` mode 0710 on the directory and 0640 on
the config file, and step 5 below now says so. `backup.env` stays `root:root` 0600,
because systemd reads it before dropping privileges. The next scheduled nightly, on
2026-09-24, succeeded end to end.

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

4. **Replace every `CHANGEME`.** The check is a sorted-key diff against the example
   file, and `ops/check_env_prod.py` IS that diff: it fails on a key the example
   declares but `.env.prod` lacks, on a `CHANGEME` left behind, on a non-empty value
   still byte-identical to the example (bar a short allow-list of fixed settings),
   and on a malformed non-empty `SENTRY_DSN`:

   ```sh
   python3 <deploy-path>/ops/check_env_prod.py \
       <deploy-path>/.env.prod <deploy-path>/.env.prod.example
   ```

   The same script runs as a deploy preflight in `ci.yml`, so a placeholder that
   slips through this step is caught again before any image lands. Acceptance: it
   prints `OK <n> keys audited`. On failure it lists key NAMES only — values are
   never printed and never recorded in evidence.

5. **Place the backup environment file** at `/etc/app-mei/backup.env` with `HC_URL`,
   `HC_OFFHOST_URL`, and the off-host credentials described under
   [Off-host copies](#off-host-copies-are-provider-neutral-and-activated-only-after-the-provider-gate).
   Confirm the path matches the refreshed unit's `EnvironmentFile=` line; the leading `-`
   makes the file optional, which protects the backup from a missing monitoring secret but
   equally means a **wrong path fails silently**. Root-owned, mode 0600. The non-secret
   AWS CLI config beside it is different: the backup service runs as the deploy user,
   so `/etc/app-mei` must be `root:<deploy-user>` mode 0710 and that file
   `root:<deploy-user>` mode 0640. Mode 0700 on the directory fails the off-host copy on
   the first night, as it did on 2026-09-23.

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

Covers PILOT-404.

#### Recorded as executed on 2026-09-22

The freeze SHA was `aff1464`, the merged cutover-preparation commit
(`.evidence/PILOT2-404E-happy.txt`). The window opened at 13:15 UTC with a rollback
deadline of 17:15 UTC. The tracked plan had set it for 2026-09-23; the owner moved it
forward to the same day. Steps 1 to 8 are in `.evidence/PILOT2-404-operator.txt`, and
steps 9 to 12 with both controls in `.evidence/PILOT2-404O2-operator.txt`.

| # | As executed |
| --- | --- |
| 1 | Apex and wildcard `A` were already at 300 s, DNS-only, with no `AAAA`; nothing to lower |
| 2 | Freeze started 14:18:55 UTC in the order caddy, beat, drain, worker. `active`, `reserved` and `scheduled` were empty, and `llen celery` was 0 |
| 3 | The final backup exited 0 and both Healthchecks checks went green. After the WAL switch there were zero `.ready` files, and `failed_count` read 0 before and after |
| 4 | Base, logical dump and WAL archive went host to host with agent-forwarded `rsync` and `scp`. SHA-256 matched three of three |
| 5 | The three volumes were removed and both Caddy volumes kept. Quarantine found zero target-made artifacts, locally or off-host |
| 6 | Restore reached the final archived segment, roles took two bootstrap passes, and `worker` and `beat` were absent |
| 7 | 99 migrations applied with none pending, RLS held by effect, row counts matched three of three with equal hashes, and the canary document's SHA-256 matched. `/readyz` was ready, `/versionz` was `aff1464`, and `/healthz` was 503 with a stale scheduler, as expected (see the corrected check 5 below) |
| 8 | `worker` and `beat` started and the timer was enabled. `/healthz` read `ok` 13 s later |
| 9 | Apex and wildcard `A` repointed to the target, DNS-only at 5 minutes, complete by 14:36:59 UTC |
| 10 | At 14:53 UTC, unpinned, from a workstation with no proxy and no hosts override: both names resolved to the target, the TLS connection landed there, the certificate came from Let's Encrypt, `/versionz` was `aff1464`, `/readyz` was ready and `/healthz` was ok |
| 11 | Old box: all five volumes present, caddy, web, worker and beat exited, db and redis running |
| 12 | The pre-window TTL was already 300 s, so nothing needed restoring. The console read back 300 |

Both controls passed. Control 2's altered count failed with exit 1 on the `documents`
line, and the restored file's hash matched the original. Control 1 needed two
corrections, both recorded:

- It ran behind the old host's security list, narrowed to the operator's address. An
  independent host was refused, and the old host's HTTP and HTTPS ingress rules were
  removed afterwards. It checked `/versionz` and `/readyz` rather than `/healthz`, which
  can't read `ok` while the old box's beat is stopped.
- The first two attempts failed. The first answered 502 before web was ready. The
  second served the wrong release, because `up` recreated the old web container from a
  stale `app-mei:prod` tag. The passing run used the retained immutable image whose
  embedded `RELEASE` is `c588669`, started with Gunicorn only and no migration command.
  **Any rollback to the old box must use that image, not the `:prod` tag.**

No rollback was invoked. The target has served the public name since the flip
(`.evidence/PILOT2-406-operator.txt`).

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
- **MUST NOT destroy old-box data.** It is the rollback anchor until seven days after
  the `v0.2.0-rc1` tag. See [Decommissioning the old host](#decommissioning-the-old-host).

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
their provenance. Quarantine by provenance: an artifact is target-made only when its STAMP
appears in the transferred set's provenance manifest, never merely because its timestamp
falls in a time interval. Capture the target backup service journal in this step:

```sh
journalctl -u app-mei-backup.service
```

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
$compose up -d db redis web caddy
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
     "https://samaronefialho.dev/readyz?cb=<window-id>" \
     | jq -e '.status == "ready" and .database == "ok" and .redis == "ok"'

   curl -fsS --resolve "samaronefialho.dev:443:$target_ip" \
     "https://samaronefialho.dev/versionz?cb=<window-id>" \
     | jq -e --arg SHA "<main-head-sha>" '.release == $SHA'

   # Expected 503 here: beat is not running yet. Recorded, not asserted green.
   curl -sS --max-time 30 --resolve "samaronefialho.dev:443:$target_ip" \
     "https://samaronefialho.dev/healthz?cb=<window-id>"
   ```

   `jq -e` is what makes these bite: a 200 carrying the wrong body exits nonzero rather
   than scrolling past. `/healthz` is not a pass condition at this step. Beat stays
   stopped until step 8, a stale scheduler is the one state that makes `/healthz` answer
   503, and on 2026-09-22 it answered 503 with `"scheduler": "stale"` exactly as
   expected. An earlier version of this step asserted `.status == "ok"`, which can't pass
   before step 8. `/healthz` must reach `"status": "ok"` after step 8, and on the day it
   did so within 13 seconds.

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
based, so they follow the flip on their own: confirm they report their expected states
rather than reconfiguring anything. Expected is **M1a–M1d UP and M1n DOWN** — see
[External uptime monitoring](#external-uptime-monitoring-four-keyword-monitors-and-a-negative-control).
M1n reporting UP here would mean the monitors followed the flip in name only.

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

Covers PILOT-406 and the old host's disposition.

**The old OCI compute instance is the deep-rollback anchor. It stays stopped and intact
until the `v0.2.0-rc1` tag exists and seven days have passed, rc1 + 7 days, and only then
is it terminated.** That schedule is owner decision D-Q3. The OCI account itself stays:
the documents bucket lives there under the keep-oci ruling, and it's never part of this
decommission.

#### Re-verification from the target, recorded as executed on 2026-09-21 to 2026-09-25

Everything proven on the old box was proven again from the new one, because evidence
gathered on a host that is about to be switched off is evidence about the wrong machine.

| # | Check | Acceptance | As executed |
| --- | --- | --- | --- |
| 1 | One email journey per type: firm invite, address verification, password reset, LGPD DSR notification | Each delivered to a real external mailbox, message-ids recorded, DKIM and SPF pass, `From` aligned | PASS on 2026-09-22, six journeys, all with SPF, DKIM and DMARC `pass` (`.evidence/PILOT2-106O-operator.txt`) |
| 2 | Backup timer fires once end to end | Local base and logical artifacts present with a fresh marker, off-host copy landed under `pg/<UTC-timestamp>/`, both Healthchecks checks green. This is the **authoritative** off-host evidence; the local MinIO rehearsal never was | FAIL on 2026-09-23, then PASS on 2026-09-24. The first scheduled nightly failed its off-host copy on a permissions fault ([bootstrap record](#new-host-bootstrap-two-phases)). An operator-started recovery run passed that morning but doesn't count as scheduled. The next scheduled nightly succeeded locally and off-host, both checks were UP, and a separate reader matched the SHA-256 (`.evidence/PILOT2-406-operator.txt`) |
| 3 | `verify-live` workflow green against the target | Scheduled or dispatched run passes, with `/versionz` release equal to `main` | PASS on 2026-09-23. The scheduled run succeeded, sent its success ping, and its Healthchecks check read UP (`.evidence/PILOT2-406-operator.txt`) |
| 4 | UptimeRobot | M1a-M1d UP against the target, and the M1n negative control DOWN. Four UP with no DOWN control does not satisfy this row, see [External uptime monitoring](#external-uptime-monitoring-four-keyword-monitors-and-a-negative-control) | PASS on 2026-09-22 (`.evidence/PILOT2-406-operator.txt`) |
| 5 | Log rotation on the target | The [Host log rotation](#host-log-rotation) checks re-run: `LogConfig` on every container, journald cap recorded | PASS on 2026-09-21 (`.evidence/PILOT2-209-operator.txt`) |
| 6 | Sentry receives a target-origin event | The event's `release` field equals the deployed SHA | PASS on 2026-09-21 at `c7afe36` (`.evidence/PILOT2-107T-operator.txt`), and again on 2026-09-24 at `699f764` (`.evidence/PILOT2-504-699f764-operator.txt`) |

The push deploy was green on the target too. The CI run for `aff1464` passed all three
jobs, including the Lightsail deploy (`.evidence/PILOT2-406-operator.txt`).

Row 2 is the one that failed, and it shows why the rule exists: a failing row gets its
remediation recorded, not a quiet retry until it passes. The off-host check also has an
independent verification half. A **separate read credential** downloads the artifact
and compares byte count and SHA-256 against the local source. The backup identity is
PutObject-only by design and cannot list or read, so it cannot verify its own work.

#### The old host now, recorded as executed on 2026-09-23

From `.evidence/PILOT2-401-operator.txt`:

- The instance was stopped gracefully, not force-stopped, on the owner's instruction
  once the OCI missed-run drill had closed. The console read `Stopped` at 10:34 UTC.
- Its 47 GB boot volume is still attached, and no block volumes are listed. Nothing was
  deleted.
- The public IP is unassigned. The last public ingress rules (two SSH, one ICMP) were
  removed, leaving only ICMP from the private network, and egress is unchanged. The HTTP
  and HTTPS rules had already gone after the cutover's control 1.
- The documents bucket was not touched. Termination has not happened.

While the instance stays in this state, rollback is still possible, but it's no longer
cheap, and the order matters:

1. Get incident approval, then freeze and drain writes on the target.
2. Reconcile data. The old host holds data as of the 2026-09-22 freeze, so any write the
   target accepted after the flip is lost unless it's carried back.
3. Reassign a public address and restore administrative ingress, because nothing can
   reach the stopped host without them.
4. Start the old web from the retained immutable image through its protected override,
   never from the stale `app-mei:prod` tag (see the [cutover
   record](#cutover-data-freeze-restore-and-dns-flip)). Validate readiness under
   restricted ingress.
5. Only then change DNS, services or public ingress.

#### Decommission schedule (D-Q3)

Termination waits for all three preconditions:

1. The `v0.2.0-rc1` tag exists. As of this record it doesn't; see
   [`RELEASES.md`](RELEASES.md).
2. The tag's date is recorded in the release row's operator artifact.
3. At least seven days have passed since that date. The earliest termination date is the
   tag date plus seven days, and it gets written down once the tag exists.

Then, in this order:

1. **Terminate the compute instance** and delete its boot volume and any block volumes.
2. **Delete OCI-era snapshots and custom images.** Keep the final cutover backup, its
   off-host copy and the cutover evidence. They outlive the machine that made them.
3. **Rotate the OCI-era credentials, by name.** Values never go in this file or in an
   evidence artifact. Each replacement is proven working before the old value is
   revoked, because revoking first turns maintenance into an outage.
   - **The OCI deploy SSH key.** Revoke it. Offered to the Lightsail host, it must be
     refused.
   - **The OCI `.env.prod` secrets.** These were carried to the target at Phase B:
     `POSTGRES_PASSWORD`, `APP_MIGRATOR_PASSWORD`, `APP_RUNTIME_PASSWORD`,
     `APP_TEST_PASSWORD`, `OCI_S3_ACCESS_KEY_ID` with `OCI_S3_SECRET_ACCESS_KEY`, and
     `CF_API_TOKEN`. The document-bucket key pair is still in live use, so create its
     replacement and pass `storage_probe` with it before revoking the old pair.
   - **The OCI API keys.** Revoke them. A call signed with an old key must answer 401.
   - `SECRET_KEY` and `EMAIL_HOST_PASSWORD` are deliberately left out of this rotation
     (`.evidence/PILOT2-401-operator.txt`, part 2 step 5).
   - The off-host writer credential in `/etc/app-mei/backup.env` also served on the OCI
     host, where the copy was first armed (`.evidence/PILOT2-301-operator.txt`), and the
     sheet doesn't list it. Rotate it with the others, or record why not.
4. **Record the result.** Append the termination time, the volume deletion, both refusal
   results, and the rotated credential names to the operator artifact, only after they
   happen. Media erasure rests on the provider's assurance. The provider's deletion
   receipt is kept privately, and no provider identifier goes into evidence.
5. **Never delete the OCI documents bucket.** It is the live document store.

### Alerting drills: Sentry, the degraded signal, and the missed ping

The Sentry DSN comes from the row-11 Sentry project and is stored in the password manager.

Covers PILOT-504.

#### Recorded as executed on 2026-09-21 to 2026-09-25

**(a) Sentry.** The project and its alert rule were set up on 2026-09-21: a new issue
mails the owner, filtered to `environment` equal to `production`
(`.evidence/PILOT2-107-operator.txt`). The same day, on Lightsail at `c7afe36`, a
`RuntimeError` raised and caught in a `manage.py` shell arrived with `release` equal
to the deployed SHA and `environment` set to `production`. It carried two traceback
frames, no frame locals, and no trace of a local canary. An empty DSN sent nothing
(`.evidence/PILOT2-107T-operator.txt`). Deviation: the event's `server_name` is the web
container's hostname, not the host's, so its origin was confirmed with `docker inspect`.
No worker-origin event was available. On 2026-09-24 at `699f764`, a synthetic event
arrived with `release` equal to `699f764` and its password field shown as `Filtered`
(`.evidence/PILOT2-504-699f764-operator.txt`).

**(b) Stop beat.** Run twice on the target.

| | 2026-09-23 at `aff1464` | 2026-09-24 at `699f764` |
| --- | --- | --- |
| Beat stopped (UTC) | 10:34:17 | 22:50:28 |
| `/healthz` 503, `"scheduler": "stale"` | 10:44:35, 15 min after the last heartbeat | 23:05:06, 15 min after the last heartbeat |
| M1b alert received | 10:46:58 | 23:09:30 |
| Monitors DOWN | M1a, M1b, M1d, **and M1c** | M1a, M1b, M1c, M1d |
| Firm page during the outage | 200 | 200 |
| `web` | `unhealthy`, `StartedAt` unchanged | `unhealthy`, `StartedAt` unchanged |
| `start beat`, then `/healthz` 200 | 10:49:56, then 10:50:26 | 23:13:20, then 23:13:26 |
| Acknowledged by the owner on call | 10:49:08 | 23:16:11, after beat was restarted to keep the outage short |
| Result | FAIL on M1c, which went DOWN on the 503 | PASS under the amended check 5 |

The first run is what corrected the M1c expectation in [External uptime
monitoring](#external-uptime-monitoring-four-keyword-monitors-and-a-negative-control)
and in row 5 of part (b) below (`.evidence/PILOT2-504-aff1464-operator.txt`). Recovery
mails arrived both times, and every monitor read UP again afterwards. A bounded re-check
on 2026-09-25 stopped beat once more and confirmed that an authenticated firm page
still rendered on `699f764` while `/healthz` answered 503.

**(c) The missed ping.** Deviation: both runs used the real backup checks, not a
rehearsal check, because no pilot users existed yet. Once the pilot starts, use the
rehearsal-check form below.

- **OCI host, 2026-09-22 to 2026-09-23** (`.evidence/PILOT2-208-operator.txt`). A
  deliberately wrong off-host credential sent the off-host check DOWN with status 5
  while the local check stayed green. A manual run with the correct credential restored
  brought it back UP.
  For the missed run, the timer was disabled under a host-local persistent recovery
  timer. Both checks went DOWN at 08:00 UTC, and the DOWN mail was received at 08:00:04.
  Re-enabling the timer started a `Persistent=` catch-up run that exited 0, and both
  checks were UP again by 08:02:51.
- **Lightsail target, 2026-09-24 to 2026-09-25**
  (`.evidence/PILOT2-504-699f764-operator.txt`). The timer was disabled at 23:26:53 UTC
  under the same kind of recovery guard, and no run happened in the expected window.
  Both checks were DOWN with a notification by 08:04:08. The backup ran again from
  08:04:19 to 08:04:35, off-host copy included, both checks read UP at 08:05:00, and the
  timer is back to `enabled`. The owner acknowledged at 08:05:44, after the recovery.

Three monitoring surfaces are configured by earlier todos. Before these drills none of
them had ever fired. A configured alert that has never been received is a belief about a delivery chain
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
| 5 | after the alert | UptimeRobot **M1b** (`"scheduler": "alive"`) alert delivered to `<alert-recipient>`, with **M1a**, **M1c** and **M1d** also DOWN, M1c on the 503 status alone | the recipient mailbox and the monitor list |
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
mapping](PILOT-RUNBOOK.md#monitor-to-action) already routes M1b's `scheduler` keyword to
the stale-scheduler entry, and this drill is what makes that row a measured path.

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

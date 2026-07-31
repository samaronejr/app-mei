# Operations

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

Because entrypoint scripts only run against an **empty** data directory, editing
`roles.sql` requires `docker compose down -v` — a restart will not re-run it.

### The portal grants do not survive a new migration

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

After any migration that adds an allow-listed table, re-run the two `GRANT` blocks
against the migrated database, as the table owner:

```sh
sed -n "/-- app_portal's allow-list./,\$p" ops/sql/roles.sql \
  | docker compose exec -T db psql "$DATABASE_MIGRATION_URL" -v ON_ERROR_STOP=1
```

**You are not expected to remember this.** `core.E012` refuses to boot when a table
roles.sql declares exists without its grant, and the production entrypoint is
`collectstatic && migrate && exec gunicorn` — `migrate` runs system checks, so the
container stops instead of the upload failing. The check reads the allow-lists out of
roles.sql rather than a copy in Python, so the two cannot drift apart.

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

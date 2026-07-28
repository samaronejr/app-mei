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
compose up -d --wait` needs no manual step.

Because entrypoint scripts only run against an **empty** data directory, editing
`roles.sql` requires `docker compose down -v` — a restart will not re-run it.

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

## Gunicorn must run the `sync` worker with `--threads 1`

Primary keys are UUIDv7 values produced by `uuid6.uuid7()`, whose monotonic counter is
documented as **not thread-safe**. Django calls a field `default` concurrently under
threaded workers, so two simultaneous inserts can be handed the same key.

```sh
gunicorn config.wsgi:application --worker-class sync --threads 1
```

This is asserted in T-022. Celery's default prefork pool is process-based and therefore
safe; if that pool is ever changed to `threads` or `gevent`, this constraint must be
revisited at the same time.

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
| Authenticated reads | `120/m` | `(tenant_id, user_id)` | `TenantMiddleware` holds a transaction open across rendering and workers run `sync --threads 1`, so unbounded GETs are a denial-of-service vector |

All four are settings (`RATELIMIT_*`), so a deployment tightens them without a code
change. Buckets live in the Redis cache.

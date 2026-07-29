# Restore runbook

Every command and number below was executed against the live staging host
(`144.33.16.182`, Oracle E2.1.Micro, sa-vinhedo-1) on 2026-07-28, not derived from
documentation. The rehearsal destroyed the database volume for real and recovered from
the archive.

## Measured figures

| Metric | Value | Where it comes from |
| --- | --- | --- |
| **RTO** | **207 s** | volume destroyed -> `db` healthy again |
| **RPO** | **<= 5 min** | `archive_timeout=300`; see the warning below |
| Base backup size | 4.4 MB gzip | near-empty database; grows with data |
| Logical dump size | 164 KB | `pg_dump -Fc` |
| WAL segment size | 16 MB each | PostgreSQL default |

### The RPO is not zero, and it is not negotiable by wishing

Recovery replays only WAL that reached `/wal_archive`. The segment currently being
written has **not** been archived, so a catastrophic loss of the data volume loses every
transaction since the last segment switch. `archive_timeout=300` forces a switch every
5 minutes *when there is activity*, which bounds the loss at roughly 5 minutes of writes.

Lowering `archive_timeout` tightens RPO but writes a full 16 MB segment each time it
fires, even a nearly empty one. On this host that is a disk-space and IO trade, not a
free win. Genuine zero-RPO needs synchronous replication to a second machine, which the
Always Free tier cannot host.

## Which restore do I want?

| Situation | Use | Why |
| --- | --- | --- |
| Volume lost, corruption, bad migration | **Physical + WAL** | Only path that recovers writes made *after* the last backup |
| Recover one table, or move major version | **Logical** | `pg_restore` is selective and version-tolerant |
| Undo a specific bad transaction | **Physical + PITR target** | Stop replay just before the damage |

---

## Path A — physical restore with WAL replay (rehearsed)

This is the procedure that was actually executed.

```sh
cd /opt/app-mei
BASE=$(ls -1 ops/backups/base | sort | tail -1)     # or pick an older one deliberately

docker compose -f docker-compose.prod.yml --env-file .env.prod down
docker volume rm app-mei_postgres_data
```

> **Do not run `down -v`.** It deletes `app-mei_wal_archive` as well, destroying the very
> WAL the restore depends on. The rehearsal removed *only* the data volume, which is also
> the realistic failure shape: the database is gone, the backups are not.

Populate `PGDATA` before PostgreSQL starts. A non-empty data directory makes the image's
entrypoint skip `initdb` and start the server, which then finds `recovery.signal` and
enters archive recovery:

```sh
docker run --rm \
  -v app-mei_postgres_data:/pgdata \
  -v app-mei_wal_archive:/wal_archive \
  -v /opt/app-mei/ops/backups:/backups \
  postgres:16 bash -c "
    set -e
    tar -xzf /backups/base/$BASE/base.tar.gz -C /pgdata
    touch /pgdata/recovery.signal
    echo \"restore_command = 'cp /wal_archive/%f %p'\" >> /pgdata/postgresql.auto.conf
    chown -R postgres:postgres /pgdata
    chmod 700 /pgdata"

docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --wait
```

### Confirm recovery really happened

A healthy container is not evidence. PostgreSQL's log is:

```sh
docker compose -f docker-compose.prod.yml --env-file .env.prod logs db \
  | grep -E "redo starts|restored log file|consistent recovery|redo done|ready to accept"
```

The rehearsal produced:

```
redo starts at 0/D000028
restored log file "00000001000000000000000E" from archive
consistent recovery state reached at 0/D000138
restored log file "00000001000000000000000F" from archive
redo done at 0/F000060
database system is ready to accept connections
```

**`restored log file ... from archive` is the load-bearing line.** Without it the server
started from the base backup alone and every write since that backup is gone — while the
container still reports healthy and the application still serves traffic.

### Point-in-time (stop before a bad transaction)

Add a target *before* first start, alongside `restore_command`:

```sh
echo "recovery_target_time = '2026-07-28 22:22:00+00'" >> /pgdata/postgresql.auto.conf
echo "recovery_target_action = 'promote'"              >> /pgdata/postgresql.auto.conf
```

Recovery pauses at the target instead of replaying to the end of the archive.

---

## Path B — logical restore

```sh
DUMP=$(ls -1 ops/backups/logical/*.dump | sort | tail -1)

docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T \
  -u postgres -e PGUSER=app_mei db \
  pg_restore --clean --if-exists --no-owner --dbname=app_mei < "$DUMP"
```

`--no-owner` matters: objects must end up owned by `app_migrator`, never `app_runtime`.
An owner bypasses its own row-level security unless the policy is `FORCE`, so restoring
ownership to the runtime role would quietly disable tenant isolation.

---

## Post-restore verification (do not skip)

A restore that returns rows but loses row-level security is worse than an outage: it
looks like success and leaks across tenants.

```sh
# 1. RLS coverage must match pre-incident (8 enabled / 8 forced / 13 tenant tables)
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T \
  -u postgres -e PGUSER=app_mei -e PGDATABASE=app_mei db psql -tAc "
select count(*) filter (where relrowsecurity) || '/' ||
       count(*) filter (where relforcerowsecurity) || '/' || count(*)
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname='public' and c.relkind='r'
  and exists (select 1 from pg_attribute a
              where a.attrelid=c.oid and a.attname='tenant_id' and not a.attisdropped)"

# 2. Roles: app_runtime must hold neither SUPERUSER nor BYPASSRLS
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T \
  -u postgres -e PGUSER=app_mei -e PGDATABASE=app_mei db psql -tAc \
  "select rolname, rolsuper, rolbypassrls from pg_roles where rolname like 'app_%' order by 1"

# 3. The isolation suite is the real proof
docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  -e TEST_DB_USER=app_test -e TEST_DB_PASSWORD="$(grep '^APP_TEST_PASSWORD=' .env.prod | cut -d= -f2- | tr -d '\"')" \
  web python -m pytest tests/isolation -q     # expect 41 passed

# 4. Application actually serves
curl -sk https://<site>/healthz     # {"status": "ok", "scheduler": "alive"}
```

---

## Traps found during the rehearsal

Each of these was hit for real, not anticipated.

1. **The archive silently did nothing for 70 segments.** `/wal_archive` is a named volume,
   so Docker created it `root:root`; postgres (uid 999) could not write. PostgreSQL
   reports this *only* as a rising `failed_count` in `pg_stat_archiver` — never a startup
   error. The server looked perfectly healthy while retaining zero WAL.
   The fix lives in the `db` entrypoint so it survives `down -v`.
   **Check `pg_stat_archiver` after any change to the db service.** `ops/backup.sh`
   refuses to run when `failed_count > 0` for exactly this reason.

2. **There is no `postgres` role.** The cluster superuser is whatever `POSTGRES_USER`
   names (`app_mei`). The OS user inside the container *is* `postgres`, so every libpq
   tool defaults to a role that does not exist. `PGUSER` must be set explicitly.

3. **`down -v` destroys the backups.** It removes `app-mei_wal_archive` too. Remove the
   single data volume instead.

4. **`pg_basebackup` runs with `--wal-method=none`** here, deliberately: the WAL comes
   from the archive, so streaming it into the backup would store every segment twice.
   A base backup on its own is therefore **not** restorable — it needs `/wal_archive`.

## Off-box copies

Everything above lives on **one host**. That is not a backup strategy; a lost instance
takes the backups with it. Oracle Always Free instances can additionally be reclaimed
when idle. Before this stops being staging, `ops/backups/` and `app-mei_wal_archive`
must be replicated somewhere else (object storage, or a second provider).

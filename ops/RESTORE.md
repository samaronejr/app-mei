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
# 1. RLS coverage must match pre-incident. Measured 2026-07-28: 8/8/13. Measured
#    2026-07-31 (throwaway rehearsal, below): 9 enabled / 9 forced / 14 tenant tables.
#    The figure grows with the schema — compare against the CURRENT source database,
#    not against a number written down here.
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
  web python -m pytest tests/isolation -q     # 41 passed 2026-07-28; 111 on 2026-07-31

# 4. Application actually serves
curl -sk https://<site>/healthz     # {"status": "ok", "scheduler": "alive"}
```

---

## Throwaway-database rehearsal

Path B restores over the live database, so it cannot be rehearsed on a running system.
This variant restores into a scratch database instead, which makes the drill repeatable
at any time and — because the source is still there to compare against — turns "the
restore succeeded" into a measurable claim. Executed 2026-07-31 against staging.

### Measured

| Step | Time |
| --- | --- |
| `ops/backup.sh` (base backup + logical dump + verify + prune) | 27 s |
| `createdb` + `pg_restore` of a 220853-byte custom-format dump | 8 s |
| Isolation suite (the control) | 703 s |

### Take the dump AFTER the change you want to see restored

The equality assertions below are only meaningful if the dump contains the rows. A dump
older than the data does not fail loudly — it produces an equality check between two
numbers that agree for the wrong reason. Assert the dump's mtime against a timestamp
taken from the data itself:

```sh
SEED=$(… psql -tAc "select floor(extract(epoch from max(created_at))) from clients_clientcompany")
DUMP=$(ls -1t ops/backups/logical/*.dump | head -1)
[ "$(stat -c %Y "$DUMP")" -gt "$SEED" ] || { echo "dump predates the data"; exit 1; }
```

### The drill

Every command runs as the cluster superuser role `app_mei`. `postgres` is only the OS
user inside the container. Paths passed to `pg_restore` are **container** paths:
`./ops/backups` is mounted at `/backups` (`docker-compose.prod.yml:127`), so a host path
produces a confusing "no such file".

```sh
cd /opt/app-mei
C='docker compose -f docker-compose.prod.yml --env-file .env.prod'
DUMP=$(basename "$(ls -1t ops/backups/logical/*.dump | head -1)")

$C exec -T -u postgres -e PGUSER=app_mei -e PGDATABASE=postgres db createdb rehearsal_scratch
$C exec -T -u postgres -e PGUSER=app_mei -e PGDATABASE=rehearsal_scratch db \
   pg_restore --no-owner --exit-on-error -d rehearsal_scratch "/backups/logical/$DUMP"
```

Then check the schema is current. **Connect as `app_mei`, not `app_migrator`:**
`--no-owner` run by `app_mei` makes `app_mei` the owner of every restored table, and
`pg_dump` omits an ACL entry that equals the owner default — so `app_migrator` ends up
with no privileges at all and the probe dies with `permission denied for table
django_migrations`, which says nothing about the schema. Schema currency is the subject
here; role fidelity is not.

```sh
$C exec -T -e DATABASE_URL="postgres://app_mei:$(grep '^POSTGRES_PASSWORD=' .env.prod \
   | cut -d= -f2- | tr -d '"')@db:5432/rehearsal_scratch" \
   web python manage.py migrate --check --skip-checks     # expect exit 0
```

### Subject and control are different claims

Keep them apart in whatever you write down, because they answer different questions and
only one of them is about the restore.

**Subject — the scratch database.** Row counts for `tenants_tenant`,
`tenants_membership` and `clients_clientcompany`, plus `relrowsecurity` /
`relforcerowsecurity` per tenant-scoped table and the `pg_policies` set. Assert the
**source count is greater than zero first**, then assert equality: `0 == 0` is a passing
comparison and an empty restore. The 2026-07-31 run measured 2 / 2 / 7 rows equal on
both sides, identical RLS flags across all 14 tenant-scoped tables (9 of them enabled
**and** forced), and 18 identical policies over 9 tables.

**Control — the cluster.** The isolation suite builds its *own* database (`test_app_mei`)
as `app_test`. It proves the cluster's roles and policies still enforce tenant isolation;
it does not touch `rehearsal_scratch` and is not evidence about the restored data.

```sh
$C exec -T -e DJANGO_SETTINGS_MODULE=config.settings.test -e TEST_DB_USER=app_test \
   -e TEST_DB_PASSWORD="$(grep '^APP_TEST_PASSWORD=' .env.prod | cut -d= -f2- | tr -d '"')" \
   web python -m pytest tests/isolation -q
```

**111 passed** on 2026-07-31 (the `41` above is the 2026-07-28 figure; the suite has
grown). Pin the assertion as a floor, not an equality, or every new isolation test turns
the runbook red.

### Prove the detector fires, then clean up

A rehearsal that only ever restores a good dump does not show that a bad one would be
caught. Truncate a **copy** and confirm the same command rejects it:

```sh
$C exec -T -u postgres -e PGUSER=app_mei db sh -c \
  "head -c 10240 /backups/logical/$DUMP > /backups/logical/truncated.dump"
$C exec -T -u postgres -e PGUSER=app_mei db pg_restore --list /backups/logical/truncated.dump
# pg_restore: error: could not read from input file: end of file   (exit 1)
```

Finish by dropping the scratch database and deleting the truncated copy — and verify
both are gone rather than assuming. Keep the dump and the base backup: they are real
backups, not rehearsal artifacts.

```sh
$C exec -T -u postgres -e PGUSER=app_mei -e PGDATABASE=postgres db dropdb rehearsal_scratch
$C exec -T -u postgres -e PGUSER=app_mei -e PGDATABASE=postgres db \
   psql -tAc "select count(*) from pg_database where datname='rehearsal_scratch'"   # 0
```

> **No cleanup step may disable, drop or work around an audit or immutability control.**
> `audit_event` and `audit_platformevent` are append-only through a trigger that binds
> every role including the superuser. If tidying up collides with it, leave the artifact
> and say so. An append-only trail whose entries can be removed by an operator cleaning
> up after themselves is not append-only.

### The backup directory can lose its owner while the container keeps running

The 2026-07-31 rehearsal could not take its dump at first: `ops/backup.sh` exited 2 at
line 47 with `mkdir: cannot create directory '/backups/base/…': Permission denied`. The
archiver precondition had passed; the failure was one step later and purely filesystem.

`ops/backups/base` and `ops/backups/logical` were owned by the host login user, mode
`755`, so postgres (uid 999) could not write into them. The entrypoint
(`docker-compose.prod.yml:112-119`) chowns `/backups` at boot precisely to prevent this,
and it had — at the container's last start, 2026-07-28. A host-side ownership change on
2026-07-29 undid it, and nothing restarted `db` afterwards: it runs the unmodified
`postgres:16` image, so no deploy ever recreates it. The nightly backup had been failing
for two days and the newest dump on the box was two days stale.

Re-apply the entrypoint's own intent, as root, in the running container — no host `sudo`
and no `down`:

```sh
$C exec -T -u root db sh -c \
  'chown postgres:postgres /backups /backups/base /backups/logical &&
   chmod 775 /backups /backups/base /backups/logical'
```

**A backup job that exits non-zero into nobody's inbox is a silent failure.** Both halves
of that sentence are now handled and the manual repair above should never be needed
again: `ops/backup.sh` re-asserts the tree's ownership from inside the container on every
run, and a successful run stamps a freshness marker that `/healthz` reports as
`"backup": "fresh"` (see [`ops/README.md`](README.md)). Nothing under `ops/backups/` is
tracked any more either — a tracked file there is unlinkable by the deploy's `git reset
--hard` once the directory belongs to postgres, which is a deploy failure rather than a
backup failure but has the same root.

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

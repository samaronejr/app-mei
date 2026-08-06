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
| Archived segment size | ~90 KB | `gzip -6`; a timeout-forced segment is ~99.5% zero padding |
| Compression cost | 46 ms/segment | measured `gzip -6` on a real 16 MiB segment |

Compression changes the restore *procedure*, not the restore *guarantees*: `RETENTION_DAYS`
and `archive_timeout` are both unchanged, so the recovery window and the RPO below are
exactly what they were.

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
    cat >> /pgdata/postgresql.auto.conf <<'EOF'
restore_command = 'if [ -f /wal_archive/%f.gz ]; then gzip -dc /wal_archive/%f.gz > %p; else cp /wal_archive/%f %p; fi'
EOF
    chown -R postgres:postgres /pgdata
    chmod 700 /pgdata"

docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --wait
```

> **Use the `cat`/heredoc, not `echo`.** The command contains `[`, `>` and `;`, and the
> quoting needed to push it through `echo` inside a `bash -c "…"` inside a shell is the
> kind of thing that half-works: a mangled `restore_command` does not fail loudly, it
> fails as "recovery found no WAL", which looks like an empty archive.

### The `restore_command` reads BOTH formats, and that is not belt-and-braces

Segments are archived compressed (`ops/…/docker-compose.prod.yml`, `archive_command`).

A `restore_command` that handles only `.gz` is therefore not "the new one", it is a
restore path that cannot read the older half of its own archive. The `if/else` above
prefers `.gz`, falls back to plain, and — importantly — still exits **non-zero** when
neither exists, which is how PostgreSQL learns it has reached the end of the archive.

Rehearsed end to end on 2026-08-01 against a throwaway cluster whose archive held ten
plain segments followed by six compressed ones; see "Rehearsing the mixed archive" below.

**The plain fallback is still required even though no plain segment remains.** The
one-time compaction of 2026-08-06 (below) converted every one, so the archive is
currently 100% `.gz` — but a base backup restored from an external copy, or a future
`archive_command` regression, reintroduces plain segments immediately. The `if/else`
costs one `test -f` per segment and removes a class of failure that presents as an
empty archive rather than as an error.

### A base backup taken before 2026-08-06 carries a restore_command that cannot work

`pg_basebackup` copies `postgresql.auto.conf` verbatim, so every base backup taken
before the compression flip contains:

```
restore_command = 'cp /wal_archive/%f %p'
```

Extract such a backup, start it, and that line is read as configuration. The archive is
now entirely `.gz`, so `cp /wal_archive/000000020000000700000041 …` finds nothing,
recovery retrieves **zero** segments, and PostgreSQL reports having reached the end of
the archive. It does not error — it stops early and looks finished, which is the exact
"recovery found no WAL" failure this file warns about at the top.

So the restore procedure's `cat >> postgresql.auto.conf` is not sufficient on its own
for a pre-2026-08-06 base. **Strip the inherited line first**, then append:

```sh
sed -i '/^restore_command/d' /pgdata/postgresql.auto.conf
```

A later assignment does win over an earlier one, so appending alone happens to work —
but only while the appended line is syntactically valid. A malformed append leaves the
*stale* line as the effective setting and recovery silently reads nothing. Deleting
first makes a broken append fail loudly instead.

### Confirm recovery really happened

A healthy container is not evidence. PostgreSQL's log is:

```sh
docker compose -f docker-compose.prod.yml --env-file .env.prod logs db \
  | grep -E "redo starts|restored log file|consistent recovery|redo done|ready to accept"
```

Two different rehearsals are quoted below, and they are **not** the same drill. Their
LSNs do not agree and are not meant to. Before you compare a live recovery against
either one, be sure which of the two you are reading.

#### Rehearsal 1: 2026-07-28, live staging host, archive was entirely plain

This is the drill named at the top of this file: the data volume was destroyed for real
and the database recovered from an archive whose every segment was plain `cp` output,
because compression did not exist yet. It is where the 207 s RTO comes from.

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
container still reports healthy and the application still serves traffic. That is the
claim this excerpt carries, and it holds whatever format the archive is in. What it
proves nothing about is the format boundary, because there was no boundary to cross.

#### Rehearsal 2: 2026-08-01, throwaway cluster, archive was mixed

Same commands, a deliberately mixed archive: ten plain segments (`…0001` to `…000A`)
followed by six compressed ones (`…000B` to `…0010`), with the base backup taken in the
plain half so that recovery has no choice but to run forward through the flip. Full
details are in "Rehearsing the mixed archive" below; these are the log lines the grep
above returns.

The full replay ran to the end of the archive:

```
restored log file "000000010000000000000003" from archive     <- plain
redo starts at 0/3000028
consistent recovery state reached at 0/3000100
...
restored log file "00000001000000000000000B" from archive     <- first compressed one
...
redo done at 0/F054F10                                        <- inside …000F, a .gz-only segment
database system is ready to accept connections
```

That restore came back with 22500 rows and an `md5` taken over every row identical to
the source, not merely a matching row count.

The point-in-time leg of the same drill replayed the same archive but stopped at a
timestamp inside the compressed half, so it ends earlier and on purpose:

```
recovery stopping before commit of transaction 755, time 2026-08-01 04:32:38.72+00
redo done at 0/E054C28                                        <- inside …000E, a .gz-only segment
```

18500 rows, and zero rows written after the target. `0/E054C28` sits inside
`00000001000000000000000E`, which in that archive exists **only** as `.gz`.

#### Rehearsal 3: 2026-08-06, live staging archive, every segment retro-compressed

The first two drills used archives built for the drill. This one used the **production
archive**, after the one-time compaction below rewrote all 1,956 of its plain segments.

Restored from the retained `20260804T060720Z` base — deliberately not the newest, so
replay had to cross ~145 segments — with the archive volume mounted **read-only**, so a
failed rehearsal could not damage the thing it was rehearsing.

```
restored log file "00000002.history" from archive
restored log file "000000020000000700000041" from archive
redo starts at 7/41000028
consistent recovery state reached at 7/41000138
redo done at 7/D4000100    system usage: elapsed: 171.59 s
selected new timeline ID: 3
archive recovery complete
```

Every segment named there exists on disk **only** as `.gz`. The proof is not the row
count on its own — it is the content checksum over the window both databases share:

| | Restored | Live |
| --- | --- | --- |
| `audit_accesslog` rows at or before `redo done` (03:32:38) | 2901 | 2901 |
| `md5` over those rows | `4f5aec06b49fdbaeb54b1cc8d80b0bb1` | `4f5aec06b49fdbaeb54b1cc8d80b0bb1` |
| user tables | 44 | 44 |

The live table held four more rows than the restore. That is not drift — they were
written after `redo done` and cannot be in it. Bounding the comparison at the recovery
point is what turns "the counts nearly match" into an equality that means something.

#### The one-time archive compaction of 2026-08-06

`archive_command` was flipped to the compressed form by recreating **only** the `db`
service, which needs no application image and therefore no deploy — worth knowing,
because on that day the disk was full and the deploy could not run.

The existing plain archive was then converted in place, one segment at a time. Each
conversion compressed to a scratch name, required a zero exit, passed `gzip -t`,
decompressed and compared **byte-for-byte** against the source, preserved `999:999` and
mode `600`, fsynced, atomically renamed, and was fetched back through the documented
`restore_command` — and only then was the plain original unlinked. Any failure at any
step left the original untouched, and the procedure was resumable after interruption.

| | Before | After |
| --- | --- | --- |
| Plain segments | 1,956 | 0 |
| Archive size | 30.56 GiB | 90.36 MiB |
| Aggregate ratio | — | **346x** |
| Disk free | 623 MB | 30.85 GiB |

**Nothing was deleted to reclaim that space.** Every segment, every base backup and
every logical dump was retained; the seven-day physical recovery window and the ≤5 min
RPO were both preserved, and `archive_timeout` is still `300`. This is recorded because
it is the one time the archive was rewritten wholesale — a future reader comparing
segment mtimes against `.backup` label dates will find them inconsistent, and this is
why.

#### Which format a recovery actually crossed

**The `restored log file` line will never say `.gz`.** PostgreSQL logs `%f`, the segment
name it asked for, not the file `restore_command` actually opened. So the log cannot tell
you whether a segment came from a compressed or a plain file, and it looks identical
either way. That is why neither excerpt above can be told apart by reading its filenames,
and it is the whole reason they have to be labelled by hand. To see which formats a
recovery genuinely crossed, compare the names in the log against the archive:

```sh
docker run --rm -v app-mei_wal_archive:/wal_archive postgres:16 \
  sh -c 'ls -1 /wal_archive | sed -n "s/\.gz$/  compressed/p; s/^\([0-9A-F]\{24\}\)$/\1  plain/p"'
```

The absence of `.gz` in the log is expected and proves nothing either way. What proves
the compressed half replayed is `redo done` landing at an LSN inside a segment that only
exists as `.gz`. Both of rehearsal 2's `redo done` lines are that: `0/F054F10` falls in
`…000F` and `0/E054C28` in `…000E`, and each of those segments exists in that archive
only as `.gz`. Rehearsal 1's `0/F000060` is a different cluster's LSN in a different
archive and carries no such claim, which is the mistake this section is arranged to
prevent.

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

## Rehearsing the mixed archive

Executed 2026-08-01 against **throwaway** clusters, never the live box and never the live
archive. Nothing in production was changed, compressed, moved or deleted by this drill.

The point of the drill is the *migration*, not the end state. An archive that is entirely
compressed proves nothing about the day the format changes, which is the only day the
restore path has to serve both. So the rig is built to straddle the boundary:

| Phase | `archive_command` | Segments produced |
| --- | --- | --- |
| base backup taken here | plain `cp` | — |
| A | plain `cp` | `…0001`–`…000A`, uncompressed |
| **container recreated** | | |
| B | gzip | `…000B`–`…0010`, `.gz` |

Recovery starts at the base backup's `START WAL` — a plain segment — and must replay
forward through the flip. Two restores were run from that one archive:

| Restore | Target | Result |
| --- | --- | --- |
| Full | end of archive | 22500 rows, `md5` of every row identical to the source |
| PITR | a timestamp inside phase B | 18500 rows, **0** rows from after the target |

The PITR log ended `recovery stopping before commit of transaction 755, time
2026-08-01 04:32:38.72+00`, with `redo done` inside `00000001000000000000000E` — a segment
that exists only as `.gz`. That is the assertion that the boundary was actually crossed;
a PITR that stopped in the plain half would have proved nothing.

> **Assert the source is non-empty before asserting equality.** `0 == 0` passes, and an
> empty restore compared against an empty source is the classic way a recovery drill
> reports success. Both restores above assert `count > 0` on the source first.

### Flipping the format needs a container recreate, not `ALTER SYSTEM`

`archive_command` is set on the server's **command line** (the `command:` list in
`docker-compose.prod.yml`), and argv outranks `postgresql.auto.conf`. So:

```sql
ALTER SYSTEM SET archive_command = '…';
SELECT pg_reload_conf();
```

returns success, writes the value into `postgresql.auto.conf`, and **changes nothing**.
Measured: `pg_settings.source` stayed `command line` and `SHOW archive_command` still
reported the old value. There is no error and no warning anywhere.

Confirm which value is actually live before believing a flip happened:

```sh
… db psql -tAc "select setting, source from pg_settings where name='archive_command'"
```

`source` must read `command line`, and the setting must be the compressed form. Changing
it for real means editing `docker-compose.prod.yml` and letting compose recreate the `db`
container — which is a short database outage, and **must not** be done with `down -v`.

The last segment the old container archives is written **plain**, during its shutdown
checkpoint, before the archiver exits. The boundary is therefore clean: every segment
belongs entirely to one format, and none is half-written across the change.

---

## Traps found during the rehearsal

Each of these was hit for real, not anticipated.

0. **A pipeline in `archive_command` loses segments silently.** `cat %p | gzip -c >
   /wal_archive/%f.gz` is the natural way to write it and it is a data-loss bug: `sh`
   returns the status of the **last** stage, so a `cat` that cannot read `%p` is
   invisible. Reproduced 2026-08-01: `archived_count` **rose**, `failed_count` stayed
   **0**, no `.ready` files were retained — and the archive held two 20-byte files that
   decompress to **zero bytes**. Two segments gone, and every signal this deployment has,
   including `ops/backup.sh`'s own `failed_count == 0` precondition, reported healthy.
   The shipped command is a `&&` chain into a `.part` file plus an atomic `mv`, so a
   failure at any stage leaves no `.gz` at all.

0b. **`failed_count` cannot see a missing archiving binary.** PostgreSQL treats exit code
   127 as fatal (`wait_result_is_any_signal(rc, include_command_not_found=true)`), so the
   archiver `ereport(FATAL)`s and dies *before* reporting the failure. Measured with
   `gzip` removed from the image: seven `archive command failed with exit code 127` lines
   in the log and `failed_count` still `0`, `last_failed_wal` still NULL. An ordinary
   non-zero exit — permission denied, disk full — logs at `LOG`, the archiver survives,
   and `failed_count` does rise. **No WAL is lost either way**: the `.ready` files are
   retained, `pg_wal` grows, and everything is archived intact once the fault clears
   (verified: the held segment decompressed to a full 16777216 bytes). The danger is that
   `pg_wal` grows unbounded on a box that is already short of disk, so `ops/backup.sh`
   now also refuses to run when segments are piling up in `pg_ls_archive_statusdir()`.

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

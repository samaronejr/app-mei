# Restore runbook

This runbook combines historical physical/WAL staging evidence with the ownership-pinned
host-loss procedure prepared for PILOT-302 and PILOT-303. The historical excerpts below
retain their rehearsal dates. Anything that depends on the new disposable rehearsals is
marked `PENDING-REHEARSAL`; do not promote a placeholder to a measured result from a
desk check or a local simulation.

## Recovery timing status

| Result | Value | Command that produces it |
| --- | --- | --- |
| Track L physical/WAL start-to-verified RTO | **6 min** (`rto_l_minutes=6`, 310 s) | `pilot-302-track-l-start.epoch` commands below; recorded in `.evidence/PILOT2-302L-operator.txt` |
| Track H off-host logical start-to-verified RTO | **98 min** (`rto_minutes=98`) | `pilot-302-track-h-start.epoch` commands below; recorded in `.evidence/PILOT2-302H-operator.txt` |
| Track H dump-age RPO at recovery start | **1.927500 h** (`rpo_hours=1.927500`) | off-host `head-object` command below; recorded in `.evidence/PILOT2-302H-operator.txt` |
| `_probe/` version recovery time | **0 s** (`object_recovery_seconds=0`) | Test B prints `object_recovery_seconds`; measured by the PILOT2-405 probe in `.evidence/PILOT2-405-operator.txt` |

Start only the track being rehearsed, immediately before its first destructive or
host-loss-recovery action. For Track L:

```sh
export RECOVERY_TRACK=l
date -u +%s > /tmp/pilot-302-track-l-start.epoch
```

For Track H, record the start **before** querying object metadata. Dump age comes from
the remote object's `LastModified`, never the download's local mtime. Python's standard
library converts the S3 timestamp without depending on GNU `date -d`:

```sh
export RECOVERY_TRACK=h
RECOVERY_START_EPOCH=$(date -u +%s)
printf '%s\n' "$RECOVERY_START_EPOCH" > /tmp/pilot-302-track-h-start.epoch
: "${OFFHOST_BACKUP_STAMP:?select the UTC backup timestamp}"
DUMP_LAST_MODIFIED=$(aws s3api head-object \
  --bucket "$OFFHOST_S3_BUCKET" \
  --key "pg/$OFFHOST_BACKUP_STAMP/logical.dump" \
  --endpoint-url "$OFFHOST_S3_ENDPOINT" \
  --region "$OFFHOST_S3_REGION" \
  --query LastModified --output text)
DUMP_EPOCH=$(python3 - "$DUMP_LAST_MODIFIED" <<'PY'
from datetime import datetime
import sys

value = sys.argv[1].replace("Z", "+00:00")
print(int(datetime.fromisoformat(value).timestamp()))
PY
)
printf 'track_h_rpo_seconds=%s\n' "$((RECOVERY_START_EPOCH - DUMP_EPOCH))"
```

Historical staging measurements remain useful for capacity planning, but they are not
PILOT-302/303 results: a near-empty base backup was 4.4 MB gzip, its logical dump was
164 KB, WAL segments were 16 MB, timeout-forced archived segments were about 90 KB at
`gzip -6`, and compression cost 46 ms per segment.

### The configured archive interval is not a measured rehearsal result

Recovery replays only WAL that reached `/wal_archive`. The segment currently being
written has **not** been archived, so a catastrophic loss of the data volume loses every
transaction since the last segment switch. `archive_timeout=300` forces a switch after
that interval when there is activity. PILOT-302 must still measure and record the
observed result; the configuration alone is not recovery evidence.

Lowering `archive_timeout` writes a full 16 MB segment each time it fires, even a nearly
empty one. On this host that is a disk-space and IO trade, not a free win. Synchronous
replication to a second machine would be a different recovery design and is not provided
by this deployment.

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
test -n "$BASE"
DATA_VOLUME=app-mei_postgres_data
test "$(docker volume inspect "$DATA_VOLUME" \
  --format '{{ index .Labels "com.docker.compose.project" }}')" = app-mei

docker compose -f docker-compose.prod.yml --env-file .env.prod down
printf 'Type REMOVE-%s to destroy only the selected data volume: ' "$DATA_VOLUME"
read -r RECOVERY_CONFIRM
test "$RECOVERY_CONFIRM" = "REMOVE-$DATA_VOLUME"
docker volume rm "$DATA_VOLUME"
```

> **Do not run `down -v`.** It deletes `app-mei_wal_archive` as well, destroying the very
> WAL the restore depends on. The rehearsal removed *only* the data volume, which is also
> the realistic failure shape: the database is gone, the backups are not.

Populate `PGDATA` before PostgreSQL starts. A non-empty data directory makes the image's
entrypoint skip `initdb` and start the server, which then finds `recovery.signal` and
enters archive recovery:

```sh
docker run --rm \
  -v "${DATA_VOLUME}:/pgdata" \
  -v app-mei_wal_archive:/wal_archive \
  -v /opt/app-mei/ops/backups:/backups \
  postgres:16 bash -c "
    set -e
    tar -xzf /backups/base/$BASE/base.tar.gz -C /pgdata
    sed -i '/^restore_command[[:space:]]*=/d' \
      /pgdata/postgresql.auto.conf
    cat >> /pgdata/postgresql.auto.conf <<'EOF'
restore_command = 'if [ -f /wal_archive/%f.gz ]; then gzip -dc /wal_archive/%f.gz > %p; else cp /wal_archive/%f %p; fi'
EOF
    touch /pgdata/recovery.signal
    chown -R postgres:postgres /pgdata
    chmod 700 /pgdata"

docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --wait
```

> **The `sed` is not optional and it must run before the append.** `pg_basebackup` copies
> `postgresql.auto.conf` verbatim, so a base taken before 2026-08-06 arrives carrying
> `restore_command = 'cp /wal_archive/%f %p'` — which now matches nothing, because the
> archive is entirely `.gz`. A later assignment does win over an earlier one, so appending
> alone happens to work *while the appended line parses*; a malformed append silently
> leaves the stale line in force and recovery reads zero segments. Deleting first makes a
> broken append fail loudly instead of quietly restoring nothing. The pattern tolerates
> whitespace around `=` because `postgresql.auto.conf` is machine-written and
> `ALTER SYSTEM` does not promise a fixed spacing.

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

The data volume was destroyed for real and the database recovered from an archive whose
every segment was plain `cp` output, because compression did not exist yet. It remains
historical evidence for the physical procedure, not a current PILOT-302 timing.

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
every logical dump was retained; the seven-day physical recovery window and
`archive_timeout=300` were both preserved. This is recorded because it is the one time
the archive was rewritten wholesale — a future reader comparing
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

## Secrets and environment

The canonical recovery source is the operator password manager at **vault `app-mei` →
item `Production recovery` → secure document `.env.prod`**. Create or confirm that item
before PILOT-302; the runbook records this location, never the document's contents. The
reconstructed host file belongs at `/opt/app-mei/.env.prod`, mode `0600`, and must never
be copied into an evidence transcript.

On a replacement host, use the password manager's secure write/paste workflow after the
empty file is installed. The final command validates Compose interpolation but suppresses
the rendered configuration because it contains secrets:

```sh
cd /opt/app-mei
umask 077
install -m 600 /dev/null .env.prod
# Write the secure document into .env.prod without printing it to the terminal.
test "$(stat -c %a .env.prod)" = 600
docker compose -f docker-compose.prod.yml --env-file .env.prod config >/dev/null
```

Compare variable **names**, never values, against `.env.prod.example` after every template
change. Missing keys fail; extra operator-only keys are permitted:

```sh
python3 - <<'PY'
from pathlib import Path

def names(path: str) -> set[str]:
    result = set()
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            result.add(line.split("=", 1)[0])
    return result

missing = sorted(names(".env.prod.example") - names(".env.prod"))
for name in missing:
    print(f"MISSING_ENV_NAME {name}")
raise SystemExit(bool(missing))
PY
```

## Roles recreation

`ops/sql/roles.sql` is idempotent: it creates missing roles, then re-asserts flags,
memberships, passwords, default privileges, and grants on every run. A new host must run
it **before** creating the restored database. Bootstrap PostgreSQL with `postgres` as the
initial database so the image does not silently create `app_mei` under the bootstrap
superuser. The second pass is the explicit idempotence control:

```sh
compose() { docker compose -f docker-compose.prod.yml --env-file .env.prod "$@"; }
export POSTGRES_DB=postgres
compose up -d db
unset POSTGRES_DB

for pass in 1 2; do
  compose exec -T -u postgres db \
    sh -ceu 'psql -U "$POSTGRES_USER" -d postgres \
      -v ON_ERROR_STOP=1 -v DBNAME=postgres' \
    < ops/sql/roles.sql
done

compose exec -T -u postgres db \
  sh -ceu 'dropdb -U "$POSTGRES_USER" --if-exists app_mei'
compose exec -T -u postgres db \
  sh -ceu 'createdb -O app_migrator -U "$POSTGRES_USER" app_mei'
```

Never let the bootstrap superuser create the target database implicitly. `app_migrator`
must own the database and every restored application table so the next migration can
alter the schema.

## Path B — off-host logical restore (ownership-pinned)

This is the total-host-loss path. Use only the logical dump downloaded from the off-host
`pg/<UTC-timestamp>/logical.dump` prefix; declare the source host unavailable for the
exercise. `OFFHOST_S3_BUCKET` is the production activation switch and is not configured
yet. PILOT-301 proved the code path only against a local MinIO stand-in, with independent
byte/hash verification in `.evidence/PILOT-301-happy.txt`; it did not contact a production
bucket.

Activate an independent recovery-reader credential/session from the password manager.
Do not reuse the backup job's PutObject-only credential. Select the timestamp explicitly,
download, assert a non-trivial file, and perform the read-only format inspection:

```sh
: "${OFFHOST_BACKUP_STAMP:?select the UTC backup timestamp}"
aws s3 cp \
  "s3://$OFFHOST_S3_BUCKET/pg/$OFFHOST_BACKUP_STAMP/logical.dump" \
  /tmp/app-mei-logical.dump \
  --endpoint-url "$OFFHOST_S3_ENDPOINT" \
  --region "$OFFHOST_S3_REGION" \
  --no-progress --only-show-errors
test "$(stat -c %s /tmp/app-mei-logical.dump)" -gt 10240
compose exec -T -u postgres db pg_restore --list < /tmp/app-mei-logical.dump >/dev/null
```

Restore through the bootstrap connection but force object creation under
`app_migrator`. All four ownership flags are load-bearing:

```sh
compose exec -T -u postgres db \
  sh -ceu 'pg_restore --clean --if-exists --no-owner --role=app_migrator \
    --exit-on-error -U "$POSTGRES_USER" --dbname=app_mei' \
  < /tmp/app-mei-logical.dump
```

Re-run the idempotent roles file **after** restore. This re-applies runtime and portal
grants to tables that did not exist during the first pass:

```sh
compose exec -T -u postgres db \
  sh -ceu 'psql -U "$POSTGRES_USER" -d app_mei \
    -v ON_ERROR_STOP=1 -v DBNAME=app_mei' \
  < ops/sql/roles.sql
```

**As executed (2026-09-22, PILOT2-302H):** this host-loss path ran end to end against
the real off-host bucket. The dump was fetched with the independent recovery-reader
credential from the password manager (`reader_credential_source=Proton Pass Production
recovery restricted reader`), not the backup job's PutObject-only credential, and the
corrupted-copy control behaved as designed: `pg_restore --list` on a truncated copy
failed with exit 1. The restore stayed ownership-pinned, executed as
`pg_restore --role=app_migrator --no-owner --no-privileges --exit-on-error`. Measured:
`rto_minutes=98`, `rpo_hours=1.927500` against dump `20260922T060811Z`,
`fetch_minutes=1`; the artifact's `readiness_timing_note` records that the clock
includes the retained attempt-2 start, the user-CA import delay, and observer sign-off.
Evidence: `.evidence/PILOT2-302H-operator.txt`.

---

## Post-restore verification (do not skip)

A restore that returns rows but loses row-level security is worse than an outage: it
looks like success and leaks across tenants.

### Ownership and migration control

The owner catalog assertion is exact: it must return **zero rows**. Do not replace it
with a count of tables or a successful connection.

```sh
UNEXPECTED_OWNERS=$(compose exec -T -u postgres db \
  sh -ceu 'psql -U "$POSTGRES_USER" -d app_mei -XAt -v ON_ERROR_STOP=1' <<'SQL'
SELECT tablename
FROM pg_tables
WHERE schemaname='public' AND tableowner <> 'app_migrator';
SQL
)
test -z "$UNEXPECTED_OWNERS" || {
  printf 'unexpected table owners:\n%s\n' "$UNEXPECTED_OWNERS"
  exit 1
}
```

Run the migration plan through `DATABASE_MIGRATION_URL`, not the runtime URL. The first
assertion is the positive control; without it, an empty result from a failed command can
look like "none pending":

```sh
MIGRATION_PLAN=$(compose run --rm --no-deps web sh -ceu '
  DATABASE_URL="$DATABASE_MIGRATION_URL" \
    python manage.py showmigrations --plan --skip-checks
')
printf '%s\n' "$MIGRATION_PLAN"
printf '%s\n' "$MIGRATION_PLAN" | grep -q '\[X\]'
if printf '%s\n' "$MIGRATION_PLAN" | grep -q '\[ \]'; then
  echo 'pending migrations found'
  exit 1
fi
```

### Role and RLS catalog controls

The role assertion also has a count control, so a missing role cannot make `bool_and`
pass over a smaller set:

```sh
ROLE_FLAGS=$(compose exec -T -u postgres db sh -ceu \
  'psql -U "$POSTGRES_USER" -d app_mei -XAt -v ON_ERROR_STOP=1' <<'SQL'
SELECT count(*) = 3 AND bool_and(
  CASE rolname
    WHEN 'app_runtime' THEN rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
    WHEN 'app_portal' THEN NOT rolcanlogin AND NOT rolinherit
                           AND NOT rolsuper AND NOT rolbypassrls
    WHEN 'app_migrator' THEN rolcanlogin AND rolbypassrls AND NOT rolsuper
    ELSE false
  END
)
FROM pg_roles
WHERE rolname IN ('app_runtime', 'app_portal', 'app_migrator');
SQL
)
test "$ROLE_FLAGS" = t
```

Every public table carrying `tenant_id` must have RLS enabled and forced. The query must
return zero rows; compare the complete policy set with the pre-incident inventory in the
operator record as well:

```sh
RLS_GAPS=$(compose exec -T -u postgres db sh -ceu \
  'psql -U "$POSTGRES_USER" -d app_mei -XAt -v ON_ERROR_STOP=1' <<'SQL'
SELECT c.relname
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
  AND EXISTS (
    SELECT 1 FROM pg_attribute AS a
    WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
  )
  AND NOT (c.relrowsecurity AND c.relforcerowsecurity);
SQL
)
test -z "$RLS_GAPS" || {
  printf 'RLS gaps:\n%s\n' "$RLS_GAPS"
  exit 1
}
```

### Restored-data and RLS-by-effect evidence

Set the known rehearsal IDs in the operator shell without printing them. `TENANT_A` and
`TENANT_B` must be distinct tenants that each own at least one restored document. The
first query proves the named firm, client, obligation, and document metadata exist while
keeping the storage key and hash out of the transcript:

```sh
: "${TENANT_A:?load known tenant A from the secure rehearsal manifest}"
: "${TENANT_B:?load known tenant B from the secure rehearsal manifest}"
: "${CLIENT_ID:?load the known client id from the secure rehearsal manifest}"
: "${OBLIGATION_ID:?load the known obligation id from the secure rehearsal manifest}"
: "${DOCUMENT_ID:?load the known document id from the secure rehearsal manifest}"
export TENANT_A TENANT_B CLIENT_ID OBLIGATION_ID DOCUMENT_ID

KNOWN_ROWS=$(compose exec -T -u postgres \
  -e TENANT_A -e CLIENT_ID -e OBLIGATION_ID -e DOCUMENT_ID db sh -ceu \
  'psql -U "$POSTGRES_USER" -d app_mei -XAt -F "|" -v ON_ERROR_STOP=1 \
    -v tenant_a="$TENANT_A" -v client_id="$CLIENT_ID" \
    -v obligation_id="$OBLIGATION_ID" -v document_id="$DOCUMENT_ID"' <<'SQL'
SELECT
  EXISTS (SELECT 1 FROM tenants_tenant WHERE id = :'tenant_a'::uuid),
  EXISTS (SELECT 1 FROM clients_clientcompany WHERE id = :'client_id'::uuid),
  EXISTS (SELECT 1 FROM obligations_obligation WHERE id = :'obligation_id'::uuid),
  EXISTS (
    SELECT 1 FROM obligations_document
    WHERE id = :'document_id'::uuid
      AND storage_key <> ''
      AND sha256 ~ '^[0-9a-f]{64}$'
  );
SQL
)
test "$KNOWN_ROWS" = 't|t|t|t'
```

Then query the **restored database itself** as `app_runtime`. Empty tenant context must
return zero rows; each populated context must return rows only for that tenant:

```sh
RLS_RESULTS=$(compose exec -T -u postgres -e TENANT_A -e TENANT_B db sh -ceu \
  'psql -U "$POSTGRES_USER" -d app_mei -qAt -v ON_ERROR_STOP=1 \
    -v tenant_a="$TENANT_A" -v tenant_b="$TENANT_B"' <<'SQL'
BEGIN;
SET LOCAL ROLE app_runtime;
SELECT set_config('app.tenant_id', '', true) AS ignored \gset
SELECT count(*) = 0 FROM obligations_document;
SELECT set_config('app.tenant_id', :'tenant_a', true) AS ignored \gset
SELECT count(*) > 0
  AND count(*) FILTER (WHERE tenant_id <> :'tenant_a'::uuid) = 0
FROM obligations_document;
SELECT set_config('app.tenant_id', :'tenant_b', true) AS ignored \gset
SELECT count(*) > 0
  AND count(*) FILTER (WHERE tenant_id <> :'tenant_b'::uuid) = 0
FROM obligations_document;
ROLLBACK;
SQL
)
test "$RLS_RESULTS" = "$(printf 't\nt\nt')"
```

These catalog and RLS-by-effect queries are the restored-database evidence.
`tests/isolation` is a separate-cluster **CONTROL**: it creates and tests
`test_app_mei` as `app_test`, never touches the restored `app_mei`, and therefore does
not prove the restore. It may still be run after the direct evidence:

```sh
TEST_DB_PASSWORD=$(python3 - <<'PY'
from pathlib import Path
for raw in Path('.env.prod').read_text().splitlines():
    if raw.startswith('APP_TEST_PASSWORD='):
        print(raw.split('=', 1)[1].strip().strip('"'))
        break
else:
    raise SystemExit('APP_TEST_PASSWORD is missing')
PY
)
export TEST_DB_PASSWORD
compose run --rm --no-deps \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  -e TEST_DB_USER=app_test -e TEST_DB_PASSWORD \
  web python -m pytest tests/isolation -q
unset TEST_DB_PASSWORD
```

Start the complete stack only after the direct checks pass, then verify the public effect
without disabling TLS verification:

```sh
compose up -d --wait
: "${RECOVERED_SITE:?set the recovered site hostname}"
curl -fsS "https://${RECOVERED_SITE}/healthz"

: "${RECOVERY_TRACK:?set to l or h at the track start}"
case "$RECOVERY_TRACK" in l|h) ;; *) exit 1 ;; esac
START_FILE="/tmp/pilot-302-track-${RECOVERY_TRACK}-start.epoch"
test -s "$START_FILE"
END_EPOCH=$(date -u +%s)
START_EPOCH=$(tr -d '\n' < "$START_FILE")
printf 'track_%s_rto_seconds=%s\n' \
  "$RECOVERY_TRACK" "$((END_EPOCH - START_EPOCH))"
```

**Executed (PILOT2-302L and PILOT2-302H):** both disposable tracks ran, and the direct
query outputs above were recorded on each. Track L was ready at 2026-09-21T22:27:04Z
(`rto_l_minutes=6`, 310 s); Track H at 2026-09-22T08:03:50Z (`rto_minutes=98`). On both
tracks: `owner_catalog_rows=0`, `migrations_applied=99` with `migrations_pending=0`,
`known_rows=t|t|t|t`, `rls_by_effect=t/t/t`, and a complete policy-inventory match of 18
policies. The five `rls_gap_rows` equal the source's five and fall inside the
owner-corrected exemption set (`non_exempt_rls_gap_rows=0`). Evidence:
`.evidence/PILOT2-302L-operator.txt` and `.evidence/PILOT2-302H-operator.txt`. No
`tests/isolation` transcript is cited as restore proof.

---

## Restored-environment identity and isolation checklist

Covers PILOT-305. **Executed 2026-09-22** inside the Track H scratch environment
(`pilot2-rehearsal-a.scratch.invalid`): all six rows passed and both negative controls
refused as designed. Per-row results, the surfaces row 5 covered, and the method
deviations are in `.evidence/PILOT2-305-operator.txt`.

The catalog and RLS-by-effect queries above prove the *database* came back with its
policies. They do not prove a person can get in, or that the application layered on top
still refuses the people it should. Those are different claims and they fail
independently: a restore can carry perfect RLS and still hand every accountant access to
every client, because the membership check that scopes a firm user to a client is
application code reading restored rows, not a policy.

So this checklist is walked through the **application**, in a browser or with an
authenticated HTTP client, inside the PILOT-302 scratch environment. Nothing here is a
`psql` query.

### Standing rules for the scratch environment

- **`ALLOWED_HOSTS` names scratch hostnames only.** Never the production apex, never the
  production wildcard, never a real firm's host. A scratch stack that answers to a
  production hostname is one stale DNS entry away from serving real users from restored
  data of unknown age.
- **Never reuse production session material.** No cookie, no session id, no CSRF token,
  and no `Authorization` header copied out of a production browser profile. Use a fresh
  private browsing profile or a fresh cookie jar. A session that authenticates against
  both environments makes the two indistinguishable in exactly the checks meant to tell
  them apart.
- **Never copy a production credential in the other direction either.** The passwords
  used below are the restored users' real passwords, exercised against a scratch stack.
  They are not written into any evidence file.
- **The environment is disposable and is destroyed at the end**, together with its
  volumes, before its `ALLOWED_HOSTS` can drift back toward anything real.

### The rows

`TOTP` in row 1 is the point of the row. The enrolled secret is a restored database row,
so a code from the operator's existing authenticator device must work without
re-enrolment. If it does not, the restore lost MFA state and every user is locked out at
cutover — a failure that no query above would have shown, because the rows are present
and the policies are intact.

| # | Actor | Action | Acceptance |
| --- | --- | --- | --- |
| 1 | Firm user (restored) | Log in with password, then answer the TOTP challenge with a code from the **already-enrolled** device | Both factors accepted, session established, no re-enrolment prompt |
| 2 | Assigned staff accountant | Open the pilot client's detail page | 200, the client's own name and data render |
| 3 | **Unassigned staff accountant** | Request the same client's detail page | **REFUSED** — see the negative control below |
| 4 | Portal user (restored) | Open the Documentos list | 200, and it lists that client's documents and no others |
| 5 | Any authenticated actor | Attempt a second tenant's data on every surface tried above | Not present anywhere: not in a list, not by direct URL, not in a search result |
| 6 | Operator | Compare the known document row's metadata against its pre-restore values | Filename, size, content type, `sha256`, and `uploaded_at` all equal |

Row 5 is deliberately phrased as "every surface tried above" rather than as one request.
Cross-tenant exposure is not usually a missing check on the detail view; it is a list
endpoint, a picker, an autocomplete, or a count in a header that was written before
scoping existed. Walk the same surfaces the earlier rows walked and assert absence on
each, recording which surfaces were covered so a later reader knows what was **not**
checked.

Row 6 compares against values captured before the restore, from the source, and held in
the secure rehearsal manifest. Comparing against whatever the restored row says is a
tautology. The `sha256` here is metadata equality only; whether the bytes behind it still
exist is [Test A](#test-a--restored-database-to-live-bucket) and is a separate claim.

### The two negative controls

A checklist of six green rows proves the application lets the right people in. It does
not prove it keeps anyone out, and a build with authorization removed entirely would pass
all six. Both controls are required; the executed run recorded both in
`.evidence/PILOT2-305-operator.txt`.

**Control 1: the unassigned accountant is refused.** A staff accountant who exists, is
authenticated, belongs to the same firm, and is simply not assigned to this client:

```sh
: "${SCRATCH_HOST:?the scratch hostname, never a production host}"
: "${PILOT_CLIENT_ID:?the known client id from the secure rehearsal manifest}"

curl -sS -o /dev/null -w '%{http_code}\n' \
  --cookie /tmp/pilot-305-unassigned.jar \
  "https://${SCRATCH_HOST}/clientes/${PILOT_CLIENT_ID}/"
```

Acceptance: the status is a refusal, and the response body carries no fragment of the
client's name or data. Record the exact status returned. **Do not accept a redirect to
the login page as a pass** without checking the session first: an expired cookie produces
the same shape as a working authorization check, and it would mean the control proved
nothing. Confirm in the same jar that row 3's actor can still reach a page they *are*
entitled to, immediately before or after, so the refusal is attributable to
authorization rather than to being logged out.

**Control 2: a cross-tenant URL probe answers 404.** The second tenant's client id,
requested with the first tenant's fully valid session:

```sh
: "${OTHER_TENANT_CLIENT_ID:?a client id belonging to the second tenant}"

curl -sS -o /dev/null -w '%{http_code}\n' \
  --cookie /tmp/pilot-305-firm.jar \
  "https://${SCRATCH_HOST}/clientes/${OTHER_TENANT_CLIENT_ID}/"
```

Acceptance: `404`. Not `403`, and the distinction is deliberate rather than cosmetic — a
`403` confirms the id exists and is somebody else's, which is a tenant-enumeration oracle
across a boundary the whole product is built to keep opaque. A `403` here is a finding,
not a pass with a note.

### Evidence

| Artifact | Contents |
| --- | --- |
| `.evidence/PILOT2-305-operator.txt` | Scratch hostname, UTC timestamps, PASS/FAIL per row, the surfaces covered by row 5, the metadata fields compared in row 6, and both negative controls with their observed status codes and the entitled-page check that makes control 1 attributable. No passwords, no TOTP secrets or codes, no session cookies, no client ids |

**Executed (PILOT2-305, 2026-09-22):** all six rows passed. Control 1's refusal was
`404` with the entitled-page check at `200`, so the refusal is attributable to
authorization; control 2's cross-tenant probe also answered `404`. Row 5 covered
dashboard counts, the client picker, client list, client search, and client detail for
the firm, assigned, unassigned, and second-tenant roles, plus the portal document list
and forbidden document direct requests, with no foreign data observed. Evidence:
`.evidence/PILOT2-305-operator.txt`.

---

## DNS

Keep the value-bearing Cloudflare export outside Git at **operator password manager →
vault `app-mei` → item `Production recovery` → secure document `Cloudflare DNS
inventory`**. The inventory must contain record type, name, target/value, proxy status,
TTL, and purpose. This runbook records only the non-secret shape:

| Record | Purpose | Recovery rule |
| --- | --- | --- |
| apex `A` (and `AAAA` only when deployed) | platform root | target comes from the replacement-host provider console |
| wildcard `A` (and matching `AAAA` only when deployed) | firm and `-portal` hosts | target must match the recovered platform host |
| existing `MX`, `TXT`, `CNAME`, and `CAA` records | mail, verification, and certificate policy | preserve exactly from the secure inventory; do not infer or "clean up" during recovery |
| `_acme-challenge` `TXT` | wildcard ACME DNS-01 | Caddy manages it through the scoped token; do not make a stale challenge record authoritative |

For a planned move, reduce DNS-only records to 300 seconds at least one old-TTL window
before cutover. Cloudflare-proxied records use Cloudflare's automatic TTL; preserve the
record's proxy status because changing it also changes the real client-IP hop count.
During an unplanned recovery, use the lowest permitted TTL and restore the inventory's
normal TTL only after TLS, `/versionz`, `/healthz`, firm-host, and portal-host checks pass.

Verify the public resolver and the recovered origin independently. Do not put the target
address into evidence; record only PASS/FAIL and UTC timestamps:

```sh
: "${APEX_DOMAIN:?set the recovered apex hostname}"
: "${FIRM_HOST:?set a known firm hostname}"
: "${PORTAL_HOST:?set its known portal hostname}"
: "${TARGET_IP:?set the replacement-host address from the provider console}"
# dig observes the resolver answer; --resolve pins each check to TARGET_IP so the
# origin is verified even while the public record still points at the lost host.
dig +short A "$APEX_DOMAIN" @1.1.1.1
dig +short A "$FIRM_HOST" @1.1.1.1
dig +short A "$PORTAL_HOST" @1.1.1.1
curl -fsS --resolve "${APEX_DOMAIN}:443:${TARGET_IP:?}" "https://${APEX_DOMAIN}/versionz"
curl -fsS --resolve "${FIRM_HOST}:443:${TARGET_IP:?}" "https://${FIRM_HOST}/healthz"
curl -fsS --resolve "${PORTAL_HOST}:443:${TARGET_IP:?}" "https://${PORTAL_HOST}/healthz"
```

**Executed in scratch form (PILOT2-302H, 2026-09-22):** the scratch environment ran with
`ALLOWED_HOSTS=.scratch.invalid`, and the firm and portal host checks passed
(`firm_host_resolves=yes`, `portal_host_resolves=yes`, `firm_login_http=200`,
`portal_http=302`). The secure record inventory and replacement-host DNS effects on the
real zone still require operator confirmation at cutover. No DNS value is recorded in
repository QA. Evidence: `.evidence/PILOT2-302H-operator.txt`.

## Object storage

Database recovery and document-byte recovery are separate claims. Test A proves a
restored row still resolves to matching live bytes. Test B proves a lost current object
can be recovered from bucket version history. Customer objects are read-only throughout;
every write/delete in Test B is confined to `_probe/`.

### Test A — restored database to live bucket

Set `CANARY_DOCUMENT_ID` from the secure rehearsal manifest without printing it. This
scratch command reads `storage_key` and `sha256` from the restored database through the
migration connection, fetches the same key with the application's S3 endpoint and runtime
storage credential, and emits no key, hash, bucket, endpoint, or credential:

```sh
: "${CANARY_DOCUMENT_ID:?load the canary id from the secure rehearsal manifest}"
export CANARY_DOCUMENT_ID
compose run --rm --no-deps -e CANARY_DOCUMENT_ID web \
  sh -ceu 'DATABASE_URL="$DATABASE_MIGRATION_URL" python -' <<'PY'
import hashlib
import hmac
import os

import boto3
import django
from django.db import connection

try:
    django.setup()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT storage_key, sha256 FROM obligations_document WHERE id = %s",
            [os.environ["CANARY_DOCUMENT_ID"]],
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("missing canary row")
    storage_key, expected_sha256 = row
    client = boto3.client(
        "s3",
        region_name=os.environ["OCI_S3_REGION"],
        endpoint_url=os.environ["OCI_S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["OCI_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["OCI_S3_SECRET_ACCESS_KEY"],
    )
    response = client.get_object(Bucket=os.environ["OCI_S3_BUCKET"], Key=storage_key)
    actual_sha256 = hashlib.sha256(response["Body"].read()).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise RuntimeError("sha256 mismatch")
except Exception:
    print("FAIL test-a restored-row-to-live-object")
    raise SystemExit(1)
print("PASS test-a restored-row-to-live-object sha256-match")
PY
```

Run the repository's globally complete, report-only reconciler against the restored
database and live bucket. The command has no delete path and no `--dry-run` option; it is
intrinsically dry-run. Suppress raw output because an anomaly line contains a storage
key, and copy only the final PASS into evidence:

```sh
RECONCILE_OUTPUT=$(compose run --rm --no-deps web sh -ceu '
  DATABASE_URL="$DATABASE_MIGRATION_URL" \
    python manage.py reconcile_documents --hash-sample-size 20
' 2>&1) || {
  printf 'FAIL reconcile_documents; inspect only in the secure operator terminal\n'
  exit 1
}
printf '%s\n' "$RECONCILE_OUTPUT" | grep -q 'missing=0'
printf '%s\n' "$RECONCILE_OUTPUT" | grep -q 'hash_mismatches=0'
if printf '%s\n' "$RECONCILE_OUTPUT" | grep -q '^CRITICAL '; then
  printf 'FAIL reconcile_documents reported a critical finding\n'
  exit 1
fi
printf 'PASS reconcile_documents rows have objects and sampled hashes match\n'
unset RECONCILE_OUTPUT
```

### Test B — recover a deleted `_probe/` object version

This script creates two versions, hides the live version behind a delete marker, proves
an ordinary authenticated read now has a 404-shaped failure, lists exact-key version
history, removes the delete marker to restore the prior live version, verifies SHA-256,
and removes every probe version in `finally`. It prints the only object-recovery timing
accepted for PILOT-303:

```sh
compose run --rm --no-deps web python - <<'PY'
import hashlib
import os
import secrets
import time

import boto3
from botocore.exceptions import ClientError

client = boto3.client(
    "s3",
    region_name=os.environ["OCI_S3_REGION"],
    endpoint_url=os.environ["OCI_S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["OCI_S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["OCI_S3_SECRET_ACCESS_KEY"],
)
bucket = os.environ["OCI_S3_BUCKET"]
key = f"_probe/recovery-{secrets.token_urlsafe(16)}"
try:
    client.put_object(Bucket=bucket, Key=key, Body=os.urandom(1024))
    live_payload = os.urandom(1024)
    live = client.put_object(Bucket=bucket, Key=key, Body=live_payload)
    if live.get("VersionId") in (None, "null"):
        raise RuntimeError("bucket versioning is not enabled")
    expected_sha256 = hashlib.sha256(live_payload).digest()

    started = time.monotonic()
    deleted = client.delete_object(Bucket=bucket, Key=key)
    marker_id = deleted.get("VersionId")
    if not deleted.get("DeleteMarker") or not marker_id:
        raise RuntimeError("delete marker was not created")
    try:
        client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if str(error.response.get("Error", {}).get("Code")) not in {"404", "NoSuchKey"}:
            raise
    else:
        raise RuntimeError("deleted probe remained readable")

    history = client.list_object_versions(Bucket=bucket, Prefix=key)
    exact_versions = [item for item in history.get("Versions", []) if item["Key"] == key]
    exact_markers = [item for item in history.get("DeleteMarkers", []) if item["Key"] == key]
    if len(exact_versions) < 2 or not any(item["VersionId"] == marker_id for item in exact_markers):
        raise RuntimeError("version history is incomplete")

    client.delete_object(Bucket=bucket, Key=key, VersionId=marker_id)
    restored = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    if hashlib.sha256(restored).digest() != expected_sha256:
        raise RuntimeError("restored version sha256 mismatch")
    elapsed = time.monotonic() - started
    print("PASS test-b delete-hidden-read version-list restore sha256-match")
    print(f"object_recovery_seconds={elapsed:.3f}")
except Exception:
    print("FAIL test-b object-version-recovery")
    raise SystemExit(1)
finally:
    try:
        history = client.list_object_versions(Bucket=bucket, Prefix=key)
        for collection in ("Versions", "DeleteMarkers"):
            for item in history.get(collection, []):
                if item["Key"] == key:
                    client.delete_object(
                        Bucket=bucket,
                        Key=key,
                        VersionId=item["VersionId"],
                    )
    except Exception:
        print("FAIL test-b cleanup")
        raise SystemExit(1) from None
PY
```

**Executed (PILOT2-303 and PILOT2-405):** Test A ran against the live bucket from the
Track H scratch stack under a read-only IAM identity (`app-documents-reader`); both
canary documents returned `document_hash_match=yes`, and the reconciler reported
`missing=0`, `hash_mismatches=0`, `reconcile_rows_without_object=0`,
`reconcile_objects_without_row=0`, `reconcile_deleted=0` (as executed with
`--hash-sample-size 5`). The object-loss leg ran as the PILOT2-405 probe with a recorded
method deviation: `CopyObject` returned HTTP 400, so the prior version was downloaded
and re-put as current; `object_recovery_seconds=0` and `restored_sha_equals_A=yes`.
Application-path object-loss coverage is in `tests/portal/test_document_consistency.py`
(14 tests passed). Bucket versioning remains a single-bucket control, not an independent
offsite copy. Evidence: `.evidence/PILOT2-303-operator.txt` and
`.evidence/PILOT2-405-operator.txt`.

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
   tool defaults to a role that does not exist. Select the connection role explicitly
   with `-U "$POSTGRES_USER"`; do not hardcode the superuser into recovery commands.

3. **`down -v` destroys the backups.** It removes `app-mei_wal_archive` too. Remove the
   single data volume instead.

4. **`pg_basebackup` runs with `--wal-method=none`** here, deliberately: the WAL comes
   from the archive, so streaming it into the backup would store every segment twice.
   A base backup on its own is therefore **not** restorable — it needs `/wal_archive`.

## Off-host copy status

`ops/backup.sh` now streams the physical base tarball and logical dump with two explicit
`aws s3 cp` operations under `pg/<UTC-timestamp>/`; it never mirrors a tree. A remote-copy
failure exits 5 and leaves the completed local-backup marker intact. `OFFHOST_S3_BUCKET`
is the activation switch and remains unset until the provider gate supplies a durable
destination and independent recovery-reader access.

The PILOT-301 happy artifact proves the repository's local MinIO copy,
Put-without-List, and independent-download/hash behavior only. It is not production
bucket evidence. The nightly off-host logical dump is the authoritative artifact for
total host loss and feeds ownership-pinned Path B. The off-host base tarball is a
best-effort extra: `pg_basebackup --wal-method=none` excludes WAL, and the WAL archive is
not continuously copied off-host, so that tarball alone cannot support host-loss recovery.

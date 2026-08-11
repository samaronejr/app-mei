#!/usr/bin/env bash
#
# Nightly backup for app-mei. Produces BOTH kinds, because they fail differently:
#
#   physical (pg_basebackup)  Restores the cluster byte-for-byte and, combined with the
#                             WAL archive, is the ONLY thing that supports point-in-time
#                             recovery. Tied to the PostgreSQL major version.
#   logical  (pg_dump -Fc)    Survives a major-version change and allows restoring a
#                             single table, but can only return you to the instant the
#                             dump began — no PITR.
#
# It also PRUNES the WAL archive. Without pruning, 16 MB segments accumulate forever and
# will fill the disk, which takes the database down — a backup job that causes the
# outage it exists to protect against. Pruning is deliberately anchored to the OLDEST
# retained base backup, not the newest: deleting WAL newer than that would silently
# render every older base backup unrestorable while appearing to succeed.
#
# On success it stamps a freshness marker into the database, which `/healthz` reads and
# reports as `"backup": "fresh"`. That marker is the whole alarm: this job's real failure
# mode is not a crash, it is a non-zero exit written into a terminal nobody is watching.
# It is written LAST and only on full success, so any failure above — including a failed
# prune — ages the marker out and becomes visible without anyone reading a log.
#
# Exit codes: 0 ok, 1 precondition failed, 2 backup failed, 3 pruning failed,
#             4 freshness marker not recorded.

set -Eeuo pipefail

hc_ping() {
  [ -n "${HC_URL:-}" ] || return 0
  curl -fsS --max-time 10 --retry 2 "$HC_URL$1" >/dev/null 2>&1 || true
}
on_exit() { rc=$?; if [ "$rc" -ne 0 ] && [ "${HC_DONE:-0}" -eq 0 ]; then hc_ping "/$rc"; fi; }
trap 'on_exit' EXIT
hc_ping "/start"

COMPOSE_DIR=${COMPOSE_DIR:-/opt/app-mei}
RETENTION_DAYS=${RETENTION_DAYS:-7}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG_TAG="app-mei-backup"

cd "$COMPOSE_DIR"
C=(docker compose -f docker-compose.prod.yml --env-file .env.prod)

log() { printf '%s [%s] %s\n' "$(date -Is)" "$LOG_TAG" "$*"; }
die() { log "FATAL: $2"; exit "$1"; }

trap 'die 2 "aborted at line $LINENO"' ERR

# --- preconditions -----------------------------------------------------------------
"${C[@]}" ps --format '{{.Service}} {{.Health}}' | grep -qx 'db healthy' \
  || die 1 "db container is not healthy; refusing to take a backup that may be inconsistent"

# The OS user inside the container is `postgres`, but the cluster SUPERUSER is whatever
# POSTGRES_USER names (app_mei here) — the stock `postgres` role does not exist. Every
# libpq tool defaults to the OS username, so PGUSER must be set explicitly or each one
# fails with `role "postgres" does not exist`.
DB_USER=$(grep -E '^POSTGRES_USER=' .env.prod | cut -d= -f2- | tr -d '"')
DB_NAME=$(grep -E '^POSTGRES_DB=' .env.prod | cut -d= -f2- | tr -d '"')
[ -n "$DB_USER" ] && [ -n "$DB_NAME" ] || die 1 "POSTGRES_USER/POSTGRES_DB missing from .env.prod"

dbx() { "${C[@]}" exec -T -u postgres -e PGUSER="$DB_USER" -e PGDATABASE="$DB_NAME" db "$@"; }

# A backup is worthless if WAL archiving is broken, because PITR silently will not work.
# Checking here turns that into a loud nightly failure instead of a discovery during an
# actual recovery.
failed=$(dbx psql -tAc "select failed_count from pg_stat_archiver" | tr -d '\r')
archived=$(dbx psql -tAc "select archived_count from pg_stat_archiver" | tr -d '\r')
log "archiver state: archived=${archived} failed=${failed}"
[ "${failed:-1}" -eq 0 ] || die 1 "WAL archiver has ${failed} failures — PITR is not working; fix before trusting backups"

# failed_count has a blind spot, and gzip in archive_command walks straight into it.
# PostgreSQL classifies exit code 127 — "command not found" — as fatal via
# wait_result_is_any_signal(rc, include_command_not_found=true), so the archiver
# ereport(FATAL)s and DIES before it can report the failure to the statistics
# collector. Measured 2026-08-01 on a throwaway cluster: gzip removed from the image,
# seven consecutive "archive command failed with exit code 127" lines in the log, and
# failed_count still 0 with last_failed_wal NULL. The check above passes and this
# script would have declared PITR healthy while nothing at all was being archived.
# (An ordinary non-zero exit — permission denied, disk full, gzip exit 1 — logs at LOG,
# the archiver survives, and failed_count does rise. That is the fault the check above
# catches, and it is the one that actually happened in July 2026.)
#
# The unmissable signal is the work queue itself: a segment stays .ready until it is
# archived, so a stalled archiver accumulates them monotonically whatever killed it.
# pg_ls_archive_statusdir() reads that queue in SQL and needs no path into PGDATA.
#
# Four is 20 minutes of archive_timeout=300 switches left unarchived. A healthy box
# clears each in well under a second (gzip -6 on a 16 MiB segment measured 46 ms), so
# the steady state is 0 or a transient 1 — this cannot fire on a burst of writes, and
# a false positive here would exit non-zero, leave the freshness marker unwritten and
# page somebody over a working backup.
pending=$(dbx psql -tAc \
  "select count(*) from pg_ls_archive_statusdir() where name like '%.ready'" | tr -d '\r')
log "archiver queue: ${pending} segment(s) pending"
[ "${pending:-99}" -le 4 ] || die 1 "${pending} WAL segments are waiting to be archived — the archiver is stalled even though failed_count is ${failed}; PITR is not working"

# --- the backup tree must be writable by postgres, on EVERY run ---------------------
# /backups is a HOST directory bind-mounted into the container (`./ops/backups`), and
# the host keeps taking it back. Measured on the staging box:
#
#   * a fresh checkout creates `ops/backups` as the ssh user;
#   * so does `git reset --hard` — the deploy's own Sync step — for any tracked path
#     inside it whose cached stat data no longer matches, which an ownership change is
#     exactly what invalidates;
#   * and if the directory is absent, Docker creates the bind-mount source as **root**.
#
# postgres inside the container is uid 999 and owns none of those, so the very first
# write fails with `mkdir: Permission denied` and the nightly backup stops. That is not
# hypothetical: it happened on 2026-07-29 23:16 and went unnoticed for two days.
#
# `ops/backups` therefore has NO tracked file in it (see .gitignore). That is the other
# half of this fix and it was bought the expensive way: once the repair below started
# chowning the directory to postgres, the deploy's `git reset --hard` could no longer
# unlink the `.gitkeep` it used to keep there and died with `Permission denied`. Git and
# postgres cannot both own that directory, and the backup has the stronger claim.
#
# So this does not try to PREVENT the host from taking the tree back. It re-asserts the
# invariant at the one moment it matters, from inside the container as root, and it is
# recursive because the entrypoint's non-recursive chown provably did not reach
# /backups/base/* — which is why the retained base backups stayed unreadable even after
# the container's own boot-time repair had run.
#
# `-c` rather than a silent `chown`: a repair means something host-side broke the tree,
# and that is worth a line in the log rather than a quiet fix that hides a recurrence.
repaired=$("${C[@]}" exec -T -u root db chown -Rc postgres:postgres /backups | wc -l | tr -d '\r') \
  || die 1 "could not assert ownership of /backups — refusing to run a backup that cannot write"
if [ "${repaired:-0}" -gt 0 ]; then
  log "REPAIRED ownership on ${repaired} path(s) under /backups — something host-side had taken the tree back"
else
  log "backup tree ownership is correct; nothing to repair"
fi

# --- physical base backup ----------------------------------------------------------
log "starting pg_basebackup -> /backups/base/${STAMP}"
dbx mkdir -p "/backups/base/${STAMP}"
dbx pg_basebackup \
    --pgdata="/backups/base/${STAMP}" \
    --format=tar --gzip --compress=6 \
    --wal-method=none \
    --checkpoint=fast \
    --no-password
# --wal-method=none is intentional: the WAL needed for recovery comes from the archive,
# so streaming it into the backup too would double-store every segment.

BASE_SIZE=$(dbx du -sh "/backups/base/${STAMP}" | cut -f1)
log "base backup complete (${BASE_SIZE})"

# --- logical dump ------------------------------------------------------------------
log "starting pg_dump -> /backups/logical/${STAMP}.dump"
dbx mkdir -p /backups/logical
dbx pg_dump --format=custom --compress=6 --no-password \
    --dbname="${DB_NAME}" --file="/backups/logical/${STAMP}.dump"
DUMP_SIZE=$(dbx du -sh "/backups/logical/${STAMP}.dump" | cut -f1)
log "logical dump complete (${DUMP_SIZE})"

# --- verify the artifacts are non-trivial ------------------------------------------
# A zero-byte or near-empty dump is the classic silent backup failure: the job "succeeds"
# and the file exists, but restores nothing.
DUMP_BYTES=$(dbx stat -c %s "/backups/logical/${STAMP}.dump" | tr -d '\r')
[ "${DUMP_BYTES:-0}" -gt 10240 ] || die 2 "logical dump is only ${DUMP_BYTES} bytes — refusing to call this a backup"
dbx pg_restore --list "/backups/logical/${STAMP}.dump" >/dev/null \
  || die 2 "pg_restore cannot read the dump we just wrote"
log "dump verified readable by pg_restore (${DUMP_BYTES} bytes)"

# --- retention ---------------------------------------------------------------------
trap 'die 3 "pruning failed at line $LINENO"' ERR
log "pruning base backups older than ${RETENTION_DAYS} days"
dbx find /backups/base -maxdepth 1 -mindepth 1 -type d -mtime "+${RETENTION_DAYS}" -exec rm -rf {} +
dbx find /backups/logical -maxdepth 1 -type f -name '*.dump' -mtime "+${RETENTION_DAYS}" -delete

# Anchor WAL pruning to the OLDEST surviving base backup.
OLDEST=$(dbx sh -c 'ls -1 /backups/base 2>/dev/null | sort | head -1' | tr -d '\r')
if [ -z "$OLDEST" ]; then
  log "no base backups retained — skipping WAL prune (keeping everything is the safe failure)"
else
  # `2>/dev/null` used to sit on this tar, and it cost the archive three days of growth.
  # The label read was failing with `Cannot open: Permission denied` — the oldest base
  # backups were owned by the host user and mode 0600, so postgres could not read the
  # very artifact it would restore from — and the discarded stderr made that
  # indistinguishable from a base backup that simply had no label. The script reported
  # "skipping prune rather than guessing", which sounds like caution and was a permission
  # error nobody could see.
  #
  # So the two cases are now separated, and BOTH are fatal. An unreadable oldest base is
  # not a pruning problem that can be deferred to tomorrow: it means the oldest point
  # this deployment can recover to does not actually exist. Exiting non-zero also leaves
  # the freshness marker unwritten, which is what turns this into an alarm rather than a
  # line in a log.
  label_stderr=$(mktemp)
  if ! LABEL=$(dbx sh -c "tar -xzOf /backups/base/${OLDEST}/base.tar.gz backup_label" 2>"$label_stderr"); then
    log "label read failed: $(tr '\n' ' ' < "$label_stderr")"
    rm -f "$label_stderr"
    die 3 "cannot read the backup label of the oldest retained base ${OLDEST} — that base backup is not restorable and the WAL prune has no safe anchor"
  fi
  rm -f "$label_stderr"

  START_WAL=$(printf '%s\n' "$LABEL" | awk '/^START WAL LOCATION/{print $6}' | tr -d '()\r')
  [ -n "$START_WAL" ] || die 3 "the label of ${OLDEST} carries no START WAL LOCATION; refusing to guess a prune anchor"

  # `-x .gz` is what makes the prune see compressed segments, and without it this
  # script would silently stop bounding the archive at all. pg_archivecleanup decides
  # what to delete with IsXLogFileName(), which demands a 24-character name, so
  # `00000001000000000000000B.gz` is not a WAL file to it and is never a candidate.
  # Dry-run on the mixed archive built for the 2026-08-01 rehearsal: without the flag
  # it listed the 10 plain segments and none of the 6 compressed ones.
  #
  # The flag is safe across BOTH formats and that is why one invocation covers a mixed
  # archive. -x only ever TRIMS a suffix that is present (TrimExtension is a tail
  # strcmp), so a plain 24-character segment passes through untouched and still
  # matches. Handling the migration therefore needs no second pass and no branch here.
  BEFORE=$(dbx sh -c 'ls -1 /wal_archive | wc -l' | tr -d '\r')
  dbx pg_archivecleanup -x .gz /wal_archive "$START_WAL"
  AFTER=$(dbx sh -c 'ls -1 /wal_archive | wc -l' | tr -d '\r')
  log "WAL pruned against oldest base ${OLDEST} (${START_WAL}): ${BEFORE} -> ${AFTER} segments"
fi

# --- freshness marker ---------------------------------------------------------------
# The last thing the run does, and the only thing anyone will notice when it stops.
# `obligations_schedulerheartbeat` is the table the T-041 dead-man's switch already uses;
# it is keyed by name precisely so more than one signal can live in it, and /healthz
# reads both rows in a single query. Written in SQL rather than through manage.py because
# this is a HOST script: it holds no Python environment and reaching one would mean
# depending on the web container to report that the database was backed up.
trap 'die 4 "could not record the freshness marker at line $LINENO — the backup itself succeeded"' ERR

# This marker carries the backup's FRESHNESS and nothing else. It used to carry free
# disk space too, and that was moved out — deliberately, not by accident.
#
# The premise was that only a host script can see the host filesystem. That premise is
# false: a container's `/` is an overlay whose upper layer lives in the daemon's snapshot
# store, so `statvfs` inside any container is answered by the filesystem where image
# layers and volumes are really written. Measured 2026-08-01T03:27Z, the worker container
# answered 16,230,788 KiB against the host's 16,230,784 KiB. The scheduler heartbeat
# therefore takes the reading now, every five minutes instead of once a night; see
# apps/obligations/heartbeat.py.
#
# The `docker info --format '{{.DockerRootDir}}'` lookup that used to live here was a
# no-op twice over on this box: /var/lib/docker is not a separate mount (it is `/` on
# /dev/sda1), and the daemon runs the containerd snapshotter, so the layers it was trying
# to point at are actually under /var/lib/containerd. Measuring the container's own
# writable layer needs neither fact to be true.
#
# The column is not written from two places. Left here as a secondary source it would
# have disagreed with the heartbeat on any night the prune actually freed space — an
# hours-old "low" against a minutes-old "ok" — and nothing in the probe could have told
# which one described the present. tests/obligations/test_disk_headroom.py reads this
# file and fails if `free_disk_kib` reappears in it.
dbx psql -v ON_ERROR_STOP=1 -qtAc "
  insert into obligations_schedulerheartbeat (name, updated_at)
  values ('backup', now())
  on conflict (name) do update set updated_at = excluded.updated_at
" >/dev/null
log "freshness marker recorded; /healthz reports backup=fresh"
hc_ping ""
HC_DONE=1

trap - ERR
log "OK  base=${BASE_SIZE} dump=${DUMP_SIZE} wal=$(dbx sh -c 'du -sh /wal_archive | cut -f1' | tr -d '\r')"
log "disk: $(df -h / | awk 'NR==2{print $4" free of "$2}')"

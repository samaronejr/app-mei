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
# Exit codes: 0 ok, 1 precondition failed, 2 backup failed, 3 pruning failed.

set -Eeuo pipefail

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
  START_WAL=$(dbx sh -c "tar -xzOf /backups/base/${OLDEST}/base.tar.gz backup_label 2>/dev/null | awk '/^START WAL LOCATION/{print \$6}' | tr -d '()'" | tr -d '\r')
  if [ -n "$START_WAL" ]; then
    BEFORE=$(dbx sh -c 'ls -1 /wal_archive | wc -l' | tr -d '\r')
    dbx pg_archivecleanup /wal_archive "$START_WAL"
    AFTER=$(dbx sh -c 'ls -1 /wal_archive | wc -l' | tr -d '\r')
    log "WAL pruned against oldest base ${OLDEST} (${START_WAL}): ${BEFORE} -> ${AFTER} segments"
  else
    log "WARNING: could not read START WAL from ${OLDEST}; skipping prune rather than guessing"
  fi
fi

trap - ERR
log "OK  base=${BASE_SIZE} dump=${DUMP_SIZE} wal=$(dbx sh -c 'du -sh /wal_archive | cut -f1' | tr -d '\r')"
log "disk: $(df -h / | awk 'NR==2{print $4" free of "$2}')"

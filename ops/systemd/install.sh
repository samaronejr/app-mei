#!/usr/bin/env bash
#
# Install (or re-install) the nightly backup timer on the host. Idempotent.
#
#   sudo ops/systemd/install.sh
#
# Deliberately NOT run by the deploy job. The deploy account's reach is already
# root-equivalent through the docker group and that is an accepted, recorded trade
# (ops/README.md); handing it `systemctl` over ssh as well would widen it for a one-time
# action. Re-run this by hand whenever the units change — the checks below make a stale
# or broken install fail here, while somebody is watching, rather than at 03:00.

set -Eeuo pipefail

UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BACKUP_SCRIPT=${BACKUP_SCRIPT:-/opt/app-mei/ops/backup.sh}
UNITS=(app-mei-backup.service app-mei-backup.timer)

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root ($UNIT_DIR and systemctl are root-only)"

# A unit whose ExecStart does not exist fails at 03:00 into nobody's inbox, which is the
# exact failure class the timer exists to end. Assert the target now, and assert it is
# the path the unit actually names rather than one this script assumes.
[ -x "$BACKUP_SCRIPT" ] || die "$BACKUP_SCRIPT is missing or not executable"
grep -qxF "ExecStart=$BACKUP_SCRIPT" "$SRC_DIR/app-mei-backup.service" \
  || die "app-mei-backup.service does not point at $BACKUP_SCRIPT"

# These units were installed on the box before they existed in the repository, so an
# overwrite could silently discard a live setting nobody had written down. Show what
# changes rather than asking the operator to trust that nothing did.
for unit in "${UNITS[@]}"; do
  if [ -e "$UNIT_DIR/$unit" ] && ! diff -u "$UNIT_DIR/$unit" "$SRC_DIR/$unit"; then
    printf '^^ replacing the installed %s with the repository copy\n\n' "$unit"
  fi
done

for unit in "${UNITS[@]}"; do
  install -m 0644 "$SRC_DIR/$unit" "$UNIT_DIR/$unit"
done

systemctl daemon-reload
systemd-analyze verify "$UNIT_DIR/app-mei-backup.timer" \
  || die "systemd rejected the units; they are on disk but nothing was enabled"
systemctl enable --now app-mei-backup.timer

systemctl --no-pager list-timers app-mei-backup.timer
printf 'installed. force a run now with: systemctl start app-mei-backup.service\n'

#!/usr/bin/env bash
# Weekly offsite backup to Datto.
#
# Unlike the daily NAS sync this does NOT delete on the destination — the
# offsite copy is the deepest tier of recovery, so historical snapshots are
# allowed to accumulate. Manage retention on the Datto side via its own
# snapshot/retention policy.

set -euo pipefail

# shellcheck disable=SC1091
[[ -f /etc/squatchtodo/backup.env ]] && source /etc/squatchtodo/backup.env

SNAPSHOT_DIR="${SNAPSHOT_DIR:-/var/lib/squatchtodo/snapshots}"
DATTO_TARGET="${DATTO_TARGET:-}"
RSYNC_OPTS_DATTO="${RSYNC_OPTS_DATTO:-}"

if [[ -z "$DATTO_TARGET" ]]; then
    echo "DATTO_TARGET not configured; set it in /etc/squatchtodo/backup.env" >&2
    exit 1
fi
if [[ ! -d "$SNAPSHOT_DIR" ]]; then
    echo "snapshot directory $SNAPSHOT_DIR missing — has the hourly job run yet?" >&2
    exit 1
fi

# shellcheck disable=SC2086
rsync -a --partial --info=stats2 $RSYNC_OPTS_DATTO \
      "$SNAPSHOT_DIR/" "$DATTO_TARGET/"

echo "weekly rsync ok: $SNAPSHOT_DIR/ -> $DATTO_TARGET/"

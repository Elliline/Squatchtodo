#!/usr/bin/env bash
# Daily off-machine backup: rsync the local snapshot directory to the NAS.
#
# Mirrors $SNAPSHOT_DIR -> $NAS_TARGET. ``--delete`` keeps the NAS aligned
# with local retention so we don't accumulate stale snapshots there.

set -euo pipefail

# shellcheck disable=SC1091
[[ -f /etc/squatchtodo/backup.env ]] && source /etc/squatchtodo/backup.env

SNAPSHOT_DIR="${SNAPSHOT_DIR:-/var/lib/squatchtodo/snapshots}"
NAS_TARGET="${NAS_TARGET:-}"
RSYNC_OPTS_NAS="${RSYNC_OPTS_NAS:-}"

if [[ -z "$NAS_TARGET" ]]; then
    echo "NAS_TARGET not configured; set it in /etc/squatchtodo/backup.env" >&2
    exit 1
fi
if [[ ! -d "$SNAPSHOT_DIR" ]]; then
    echo "snapshot directory $SNAPSHOT_DIR missing — has the hourly job run yet?" >&2
    exit 1
fi

# --partial keeps interrupted transfers resumable; --info=stats2 prints a
# summary line into the journal so we can audit success without verbose noise.
# shellcheck disable=SC2086
rsync -a --delete --partial --info=stats2 $RSYNC_OPTS_NAS \
      "$SNAPSHOT_DIR/" "$NAS_TARGET/"

echo "daily rsync ok: $SNAPSHOT_DIR/ -> $NAS_TARGET/"

#!/usr/bin/env bash
# Hourly online backup of the SquatchTodo SQLite database.
#
# Uses ``sqlite3 .backup`` which is atomic and consistent without stopping the
# service. Snapshots are written to $SNAPSHOT_DIR with hour-precision UTC
# filenames; older snapshots beyond $HOURLY_RETENTION are pruned.
#
# Failures are loud: set -e + exit non-zero so the systemd unit is marked
# failed and shows up in ``systemctl --failed``.

set -euo pipefail

# Defaults — override via /etc/squatchtodo/backup.env
DB_PATH="${DB_PATH:-/var/lib/squatchtodo/squatchtodo.db}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-/var/lib/squatchtodo/snapshots}"
HOURLY_RETENTION="${HOURLY_RETENTION:-48}"

# shellcheck disable=SC1091
[[ -f /etc/squatchtodo/backup.env ]] && source /etc/squatchtodo/backup.env

if [[ ! -f "$DB_PATH" ]]; then
    echo "database not found at $DB_PATH" >&2
    exit 1
fi

mkdir -p "$SNAPSHOT_DIR"

ts="$(date -u +%Y-%m-%dT%H)"
dest="$SNAPSHOT_DIR/squatchtodo-${ts}.db"
tmp="${dest}.partial"

# Write to a temp name, then rename — keeps a half-written file from being
# picked up by the rsync stages if the script is killed mid-backup.
sqlite3 -bail "$DB_PATH" ".backup '$tmp'"
mv -f "$tmp" "$dest"

# Prune by mtime so a wall-clock skew doesn't leave gaps. ``ls -1t`` is the
# simplest portable approach; a future-dated snapshot still sorts first and
# survives until newer ones bury it.
#
# We avoid ``mapfile`` (bash 4+) so this script runs on any bash. When a
# .db file is pruned, its -wal/-shm sidecars (if any) go with it.
ls -1t "$SNAPSHOT_DIR"/squatchtodo-*.db 2>/dev/null \
  | tail -n +"$((HOURLY_RETENTION + 1))" \
  | while IFS= read -r old; do
      rm -f -- "$old" "${old}-wal" "${old}-shm"
    done

size=$(wc -c < "$dest" | tr -d ' ')
echo "backup ok: $dest (${size} bytes, retention=$HOURLY_RETENTION)"

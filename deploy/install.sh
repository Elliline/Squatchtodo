#!/usr/bin/env bash
# Install SquatchTodo on a fresh Fedora / SquatchOS box.
#
# Run as root from anywhere — the script discovers the repo root from its own
# location. Idempotent: safe to re-run after pulling new code; it rsyncs the
# source tree, reinstalls the package in the venv, and reloads systemd.
#
# Usage:
#   sudo ./deploy/install.sh
#
# Post-install:
#   sudo systemctl enable --now squatchtodo
#   journalctl -u squatchtodo -f

set -euo pipefail

# --- guards ----------------------------------------------------------------

if [[ "$(uname)" != "Linux" ]]; then
    echo "this installer targets Linux (Fedora/SquatchOS); aborting on $(uname)" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "must be run as root (sudo)" >&2
    exit 1
fi

# --- layout ---------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

INSTALL_DIR=/opt/squatchtodo
CONFIG_DIR=/etc/squatchtodo
DATA_DIR=/var/lib/squatchtodo
SERVICE_USER=squatchtodo
SERVICE_GROUP=squatchtodo

PY=${PY:-/usr/bin/python3}
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "python3 not found at $PY; install python3 (>=3.12) first" >&2
    exit 1
fi

# --- 1. system user --------------------------------------------------------

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system \
            --home-dir "$INSTALL_DIR" \
            --shell /sbin/nologin \
            --comment "SquatchTodo service" \
            "$SERVICE_USER"
    echo "created system user $SERVICE_USER"
else
    echo "system user $SERVICE_USER already exists — leaving alone"
fi

# --- 2. directories --------------------------------------------------------

install -d -o root -g "$SERVICE_GROUP" -m 0750 "$CONFIG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0755 "$INSTALL_DIR"

# btrfs subvolume for the data dir per PROJECT.md, so snapshots are
# independent. Falls back to a plain directory if btrfs isn't usable here.
if [[ ! -d "$DATA_DIR" ]]; then
    parent="$(dirname "$DATA_DIR")"
    if command -v btrfs >/dev/null 2>&1 \
       && [[ "$(stat -f -c %T "$parent" 2>/dev/null || true)" == "btrfs" ]]; then
        btrfs subvolume create "$DATA_DIR"
        echo "created btrfs subvolume at $DATA_DIR"
    else
        install -d -m 0750 "$DATA_DIR"
        echo "created plain directory at $DATA_DIR (parent fs is not btrfs)"
    fi
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR"
chmod 0750 "$DATA_DIR"

# --- 3. sync source --------------------------------------------------------

rsync -a --delete \
      --exclude '.git' \
      --exclude '.venv' \
      --exclude '__pycache__' \
      --exclude '.pytest_cache' \
      --exclude '.mypy_cache' \
      --exclude '.ruff_cache' \
      --exclude '*.db' --exclude '*.db-shm' --exclude '*.db-wal' \
      --exclude 'tests' \
      "$REPO_ROOT/" "$INSTALL_DIR/"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"

# --- 4. python venv + package install -------------------------------------

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    sudo -u "$SERVICE_USER" "$PY" -m venv "$INSTALL_DIR/.venv"
    echo "created venv at $INSTALL_DIR/.venv"
fi

# Editable install so a re-run of this script after a git pull picks up code
# changes without reinstalling. Production-grade lock would require a
# constraints.txt / requirements.lock — skipped for v1.
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"

# --- 5. config -------------------------------------------------------------

if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    install -o root -g "$SERVICE_GROUP" -m 0640 \
        "$REPO_ROOT/deploy/config.example.toml" \
        "$CONFIG_DIR/config.toml"
    echo "installed default config at $CONFIG_DIR/config.toml — review before starting"
else
    echo "config already exists at $CONFIG_DIR/config.toml — leaving alone"
fi

# --- 6. migrate -----------------------------------------------------------

sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/squatchtodo-migrate" \
    --db "$DATA_DIR/squatchtodo.db"

# --- 7. systemd unit + backup units ---------------------------------------

# Main service
install -o root -g root -m 0644 \
    "$REPO_ROOT/deploy/squatchtodo.service" \
    /etc/systemd/system/squatchtodo.service

# Backup scripts must be executable by the squatchtodo user.
chmod 0755 \
    "$INSTALL_DIR/deploy/backup-hourly.sh" \
    "$INSTALL_DIR/deploy/backup-daily.sh" \
    "$INSTALL_DIR/deploy/backup-weekly.sh"

# Backup units (services + timers). Timers are NOT enabled here — daily and
# weekly need $NAS_TARGET / $DATTO_TARGET set in /etc/squatchtodo/backup.env
# first, and the user should review the cadence. Hourly is safe to enable
# right away once the main service is up.
for u in squatchtodo-backup-hourly.service \
         squatchtodo-backup-hourly.timer   \
         squatchtodo-backup-daily.service  \
         squatchtodo-backup-daily.timer    \
         squatchtodo-backup-weekly.service \
         squatchtodo-backup-weekly.timer; do
    install -o root -g root -m 0644 \
        "$REPO_ROOT/deploy/$u" \
        "/etc/systemd/system/$u"
done

# Backup env: install the example only if the operator hasn't created one yet.
if [[ ! -f "$CONFIG_DIR/backup.env" ]]; then
    install -o root -g "$SERVICE_GROUP" -m 0640 \
        "$REPO_ROOT/deploy/backup.env.example" \
        "$CONFIG_DIR/backup.env"
    echo "installed default backup.env at $CONFIG_DIR/backup.env — set NAS_TARGET / DATTO_TARGET before enabling daily/weekly timers"
else
    echo "backup.env already exists at $CONFIG_DIR/backup.env — leaving alone"
fi

systemctl daemon-reload

cat <<EOF

install complete.

  config:        $CONFIG_DIR/config.toml
  backup config: $CONFIG_DIR/backup.env
  data:          $DATA_DIR/squatchtodo.db
  snapshots:     $DATA_DIR/snapshots/
  service:       /etc/systemd/system/squatchtodo.service
  logs:          journalctl -u squatchtodo

next:
  sudo systemctl enable --now squatchtodo
  curl -fsS http://127.0.0.1:3100/healthz

  # Once the main service is healthy, enable hourly local snapshots:
  sudo systemctl enable --now squatchtodo-backup-hourly.timer

  # After editing $CONFIG_DIR/backup.env with NAS / Datto targets:
  sudo systemctl enable --now squatchtodo-backup-daily.timer
  sudo systemctl enable --now squatchtodo-backup-weekly.timer

EOF

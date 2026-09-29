#!/usr/bin/env bash
# Restore the databases from a nightly backup:  bash deploy/restore-backup.sh 2026-10-01
set -euo pipefail
DAY="${1:?usage: restore-backup.sh YYYY-MM-DD}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA_DIR="$(grep -E '^ABG_DATA_DIR=' "$APP_DIR/.env" | cut -d= -f2- || true)"
DATA_DIR="${DATA_DIR:-$HOME/.abg-terminal}"
SRC="$DATA_DIR/backups/$DAY"
[ -d "$SRC" ] || { echo "no backup at $SRC"; ls "$DATA_DIR/backups"; exit 1; }
sudo systemctl stop abg
for f in portfolio.sqlite3 signals.sqlite3; do
  [ -f "$SRC/$f" ] && cp "$DATA_DIR/$f" "$DATA_DIR/$f.before-restore" 2>/dev/null || true
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DATA_DIR/$f" && rm -f "$DATA_DIR/$f-wal" "$DATA_DIR/$f-shm"
done
sudo systemctl start abg
echo "restored $DAY (previous files kept as *.before-restore)"

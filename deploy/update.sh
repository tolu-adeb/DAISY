#!/usr/bin/env bash
# Pull the latest code, reinstall, restart and health-check.   bash ~/abg/deploy/update.sh
set -euo pipefail
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PORT="${PORT:-8000}"
git -C "$APP_DIR" pull --ff-only
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[all]"
sudo systemctl restart abg
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 3
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null; then echo "updated and healthy ($(git -C "$APP_DIR" log -1 --format='%h %s'))"; exit 0; fi
done
echo "not healthy after restart; last logs:"; journalctl -u abg -n 40 --no-pager; exit 1

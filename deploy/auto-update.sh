#!/usr/bin/env bash
# Nightly auto-update for a DAISY server (installed by setup-server.sh as abg-update.timer; docs/20.4).
# Pulls main; if anything changed, installs and runs the test suite in a scratch copy first, and only then
# fast-forwards the live checkout and restarts the service.  A red suite leaves the running version alone.
#   run by hand:  sudo systemctl start abg-update.service      log: journalctl -u abg-update -n 50
set -euo pipefail
APP_DIR="${APP_DIR:?}"; APP_USER="${APP_USER:?}"; PORT="${PORT:-8000}"
as_user() { runuser -u "$APP_USER" -- "$@"; }
as_user git -C "$APP_DIR" fetch -q origin main
LOCAL=$(as_user git -C "$APP_DIR" rev-parse HEAD); REMOTE=$(as_user git -C "$APP_DIR" rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then echo "up to date ($LOCAL)"; exit 0; fi
if ! as_user git -C "$APP_DIR" merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
  echo "main was rewritten or the server has local commits - not updating; fix by hand"; exit 1; fi
TEST_DIR=$(mktemp -d); chown "$APP_USER" "$TEST_DIR"
trap 'rm -rf "$TEST_DIR"' EXIT
as_user git clone -q "$APP_DIR" "$TEST_DIR/daisy"
as_user git -C "$TEST_DIR/daisy" checkout -q "$REMOTE"
as_user python3 -m venv "$TEST_DIR/venv"
as_user "$TEST_DIR/venv/bin/pip" install -q -e "$TEST_DIR/daisy[all]" pytest pytest-asyncio
if ! as_user env -C "$TEST_DIR/daisy" ABG_DATA_DIR="$TEST_DIR/data" "$TEST_DIR/venv/bin/python" -m pytest -q -x -p no:cacheprovider; then
  echo "tests failed on $REMOTE - staying on $LOCAL"; exit 1; fi
as_user git -C "$APP_DIR" merge -q --ff-only "$REMOTE"
as_user "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[all]"
systemctl restart abg
for i in $(seq 1 10); do
  sleep 3
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null; then
    echo "updated $LOCAL -> $(as_user git -C "$APP_DIR" log -1 --format='%h %s') and healthy"; exit 0; fi
done
echo "not healthy after the update - rolling back to $LOCAL"
as_user git -C "$APP_DIR" reset -q --hard "$LOCAL"
as_user "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[all]"
systemctl restart abg
exit 1

#!/usr/bin/env bash
# Server half of deploy/move-to-vps.ps1 (docs/20.4).  Runs on the VPS from ~/daisy-move/:
#   daisy-server.tgz   data + .env from the PC (holds keys: deleted at the end)
#   vera/              projects.yaml + cards/daisy.md for VERA (optional)
set -euo pipefail
MOVE="$HOME/daisy-move"
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

if systemctl is-active --quiet abg 2>/dev/null; then
  say "abg is already running here - keeping its data (only updating code and VERA)"
  FRESH=0
else
  FRESH=1
fi

if [ "$FRESH" = 1 ]; then
  say "unpacking DAISY's data and settings"
  [ -d "$HOME/abg-data" ] && mv "$HOME/abg-data" "$HOME/abg-data.before-$(date +%Y%m%d-%H%M%S)"
  tar -xzf "$MOVE/daisy-server.tgz" -C "$HOME"            # -> ~/abg-data, ~/.env.server.local
fi
rm -f "$MOVE/daisy-server.tgz"

say "installing DAISY (deploy/setup-server.sh)"
curl -fsSL https://raw.githubusercontent.com/tolu-adeb/DAISY/main/deploy/setup-server.sh -o "$MOVE/setup-server.sh"
REPO_SSH=https://github.com/tolu-adeb/DAISY.git bash "$MOVE/setup-server.sh"
rm -f "$HOME/.env.server.local"

say "health"
ok=0
for i in $(seq 1 20); do
  if curl -fsS localhost:8000/api/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
[ "$ok" = 1 ] && echo "DAISY is up on the server" || { echo "DAISY didn't answer - journalctl -u abg -n 60 --no-pager"; journalctl -u abg -n 30 --no-pager || true; }
systemctl list-timers abg-update.timer --no-pager | head -3 || true

if [ -d "$MOVE/vera" ]; then
  say "updating VERA's DAISY settings"
  targets=()
  [ -d /opt/vera/config ] && targets+=(/opt/vera)
  while IFS= read -r f; do targets+=("$(dirname "$(dirname "$f")")"); done < <(find "$HOME" -maxdepth 4 -path '*/config/projects.yaml' -not -path '*/abg/*' 2>/dev/null | grep -i vera || true)
  if [ ${#targets[@]} = 0 ]; then echo "no VERA install found (looked in /opt/vera and your home folder)"; fi
  for d in "${targets[@]}"; do
    sudo cp "$MOVE/vera/projects.yaml" "$d/config/projects.yaml"
    sudo cp "$MOVE/vera/daisy.md" "$d/cards/daisy.md"
    [ -d /opt/vera ] && [ "$d" = /opt/vera ] && sudo chown vera:vera "$d/config/projects.yaml" "$d/cards/daisy.md"
    echo "updated $d"
  done
  if [ -x /opt/vera/.venv/bin/python ]; then
    sudo -u vera bash -c 'cd /opt/vera && .venv/bin/python -m vera --config-dir /opt/vera/config check-config' || true
  fi
fi
rm -rf "$MOVE"
say "done"
echo "Dashboard (private): sudo tailscale serve --bg 8000   then open this server's Tailscale name"
echo "Tokens: grep TOKEN ~/abg/.env      Logs: journalctl -u abg -f      Updates: journalctl -u abg-update -n 50"

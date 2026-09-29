#!/usr/bin/env bash
# One-shot, re-runnable setup for an Ubuntu 22.04/24.04 server (Oracle Cloud A1, Hetzner, a home box ...).
#   curl -fsSL <raw url of this file> | bash        or        bash deploy/setup-server.sh
# What it does (safe to run again; it only adds what's missing):
#   1 packages + automatic security updates + 2 GB swap on small machines + journald size cap
#   2 GitHub access (read-only deploy key for a private repo) and clone / update the code
#   3 Python venv + install
#   4 .env with fresh admin/view tokens (chmod 600) and a data folder
#   5 systemd service "abg" (starts on boot, restarts on crash, sandboxed)
#   6 Tailscale: install; `tailscale serve` publishes the dashboard privately once you've logged in
set -euo pipefail
REPO_SSH="${REPO_SSH:-git@github.com:tolu-adeb/DAISY.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-$HOME/abg}"
DATA_DIR="${DATA_DIR:-$HOME/abg-data}"
PORT="${PORT:-8000}"
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

say "1/6 packages, security updates, swap, log limits"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl python3-venv python3-pip sqlite3 unattended-upgrades >/dev/null
sudo dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true
if [ "$(free -m | awk '/Mem:/{print $2}')" -lt 2500 ] && ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/abg.conf >/dev/null
sudo systemctl restart systemd-journald || true

say "2/6 GitHub access + code"
if [ ! -d "$APP_DIR/.git" ]; then
  if ! ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    [ -f "$HOME/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -C "abg-server deploy key" -f "$HOME/.ssh/id_ed25519" >/dev/null
    echo
    echo "This server needs read access to the private repo. Add this key on GitHub:"
    echo "  repo -> Settings -> Deploy keys -> Add deploy key (title: abg-server, leave 'Allow write' OFF)"
    echo
    cat "$HOME/.ssh/id_ed25519.pub"
    echo
    echo "Then run this script again."
    exit 2
  fi
  git clone -q --branch "$BRANCH" "$REPO_SSH" "$APP_DIR"
else
  git -C "$APP_DIR" pull -q --ff-only
fi

say "3/6 Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[all]"

say "4/6 configuration"
mkdir -p "$DATA_DIR"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  {
    echo ""
    echo "# ---- set by setup-server.sh"
    echo "ABG_DATA_DIR=$DATA_DIR"
    echo "ABG_CACHE_DIR=$DATA_DIR/cache"
    echo "ABG_ADMIN_TOKEN=$(openssl rand -hex 24)"
    echo "ABG_VIEW_TOKEN=$(openssl rand -hex 24)"
  } >> "$APP_DIR/.env"
  NEW_ENV=1
fi
chmod 600 "$APP_DIR/.env"

say "5/6 systemd service"
sudo tee /etc/systemd/system/abg.service >/dev/null <<UNIT
[Unit]
Description=ABG Intelligence Terminal (dashboard, monitor, signal tracking)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/abg serve --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=10
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1
Environment=MPLCONFIGDIR=$DATA_DIR/.mpl
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$DATA_DIR $APP_DIR $HOME/.abg-terminal $HOME/.cache/abg-terminal
LimitNOFILE=8192

[Install]
WantedBy=multi-user.target
UNIT
mkdir -p "$HOME/.abg-terminal" "$HOME/.cache/abg-terminal"
sudo systemctl daemon-reload
sudo systemctl enable -q abg
sudo systemctl restart abg
sleep 6
if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null; then echo "service is up"; else
  echo "service didn't answer yet; check: journalctl -u abg -n 50 --no-pager"; fi

say "6/6 Tailscale (private access)"
if ! command -v tailscale >/dev/null; then curl -fsSL https://tailscale.com/install.sh | sh >/dev/null; fi
if tailscale status >/dev/null 2>&1; then
  sudo tailscale serve --bg "$PORT" >/dev/null && tailscale serve status || true
else
  echo "Log this server into Tailscale:   sudo tailscale up --ssh"
  echo "then publish the dashboard:        sudo tailscale serve --bg $PORT"
fi

echo
echo "Done. Next steps (docs/12-deployment.md):"
[ "${NEW_ENV:-0}" = 1 ] && echo "  * nano $APP_DIR/.env   -> paste your API keys / Discord settings, then: sudo systemctl restart abg"
echo "  * your dashboard tokens:  grep TOKEN $APP_DIR/.env"
echo "  * logs: journalctl -u abg -f      status: systemctl status abg      update: bash $APP_DIR/deploy/update.sh"

# 13. Deployment: always on, private, backed up

The target is a small always-on Linux server (Oracle Cloud Always Free Ampere A1, or any Ubuntu
22.04/24.04 box):
- the terminal runs as a **systemd service** listening only on `127.0.0.1`
- it is reachable only through **Tailscale** (no public ports)
- it's protected by **admin / view tokens**, **backed up nightly**, and watched by a **dead-man's switch**

```
 your laptop / phone ──Tailscale (WireGuard)──► server: tailscale serve :443 ──► 127.0.0.1:8000  abg serve
                                                         systemd: restart on crash / boot
 Discord ◄── relays, recaps, alerts ─────────────────────────┘      nightly backups · healthchecks.io ping
```

## 13.1 One-time setup

1. **Create the server.** Oracle: Ubuntu 24.04 (aarch64), `VM.Standard.A1.Flex` 1–2 OCPU / 6–12 GB,
   a public subnet with a public IPv4, and your SSH public key. Consider switching the account to
   Pay-As-You-Go with a $1 budget alert, so an "idle" Always Free server isn't reclaimed.
2. **Log in:** `ssh ubuntu@<public-ip>`.
3. **Run the setup script** (safe to re-run):
   ```bash
   curl -fsSLO https://raw.githubusercontent.com/tolu-adeb/DAISY/main/deploy/setup-server.sh   # public repo
   # private repo: copy the file over instead:  scp deploy/setup-server.sh ubuntu@<ip>:~
   bash setup-server.sh
   ```
   The first run prints a **deploy key** and stops. Add it on GitHub (repo → Settings → Deploy keys →
   Add, read-only), then run the script again. It then:
   - installs packages, automatic security updates, swap and a log size cap
   - clones the repo to `~/abg`, creates the venv and installs everything
   - writes `~/abg/.env` (`chmod 600`) with fresh `ABG_ADMIN_TOKEN` / `ABG_VIEW_TOKEN` and
     `ABG_DATA_DIR=~/abg-data`
   - installs and starts the `abg` systemd service
   - installs Tailscale
4. **Tailscale:**
   ```bash
   sudo tailscale up --ssh          # open the printed link, sign in (same account as your laptop/phone)
   sudo tailscale serve --bg 8000   # https://<server-name>.<tailnet>.ts.net -> the dashboard, tailnet only
   ```
5. **Secrets:** run `nano ~/abg/.env` and paste your API keys and Discord settings (the same values as
   on your PC). Then `sudo systemctl restart abg`.
6. **Check:**
   - `systemctl status abg`
   - `journalctl -u abg -f`
   - `curl -s localhost:8000/api/health`
   - open the Tailscale URL, then click 🔒 and paste the admin token (`grep TOKEN ~/abg/.env`)

## 13.2 Lock it down

- **Close public SSH** once `ssh ubuntu@<server-name>` over Tailscale works: Oracle console → your VCN
  → Security Lists → delete the ingress rule for port 22 (0.0.0.0/0). From then on the server has no
  public ports at all.
- **Tokens.** Anyone you share the tailnet or the machine with needs a token.
  - Give them the **view token**: they can see everything but change nothing.
  - Keep the **admin token** to yourself. Every change through the dashboard or API needs it.
- **Discord.** Only user ids in `ABG_DISCORD_ADMIN_IDS` can use the changing slash commands. Keep the
  relay webhook URL private: anyone with it can post in that channel.
- **GitHub.** Keep the repo private. Collaborators get Read, and the server's deploy key is read-only.
- **Secrets never leave the box.** `.env` is git-ignored and `chmod 600`. If a key ever leaks, rotate
  it at the provider and update `.env`.

## 13.3 Dead-man's switch (free)

The monitor can't report on the whole server going down, so an outside service watches it:
1. https://healthchecks.io → new check, period 5 minutes, grace 10 minutes, email and/or Discord
   integration.
2. Set `ABG_HEALTHCHECK_URL=https://hc-ping.com/<uuid>` in `.env`, then restart the service.

The monitor pings every 5 minutes. It sends `/fail` when quotes have stopped or the price stream has
been down 10+ minutes in market hours. If the pings stop (crash, reboot loop, deleted server), you
get the alert.

## 13.4 Backups and restore

- **Nightly:** online SQLite backups at `ABG_BACKUP_HOUR` (02:00 ET) to
  `~/abg-data/backups/YYYY-MM-DD/`, keeping 14 days.
- **Now:** `~/abg/.venv/bin/abg backup`.
- **Restore:** `bash ~/abg/deploy/restore-backup.sh 2026-10-01`. It stops the service, keeps the
  current files as `*.before-restore`, and restarts.
- **Off-server copy (recommended):** from your PC, run
  `scp -r ubuntu@<server-name>:~/abg-data/backups ./abg-backups` now and then.

## 13.5 Updating

Push to GitHub from your PC, then on the server run:
```bash
bash ~/abg/deploy/update.sh      # git pull, reinstall, restart, health check (prints logs if unhealthy)
```

## 13.6 Running only one copy

The server should be the only process tracking and posting. On your PC:
- set `ABG_MONITOR_ON_SERVE=false`, and don't run `Start Monitor.bat`
- or just use the server's dashboard through Tailscale

Two copies with the same bot and webhook post everything twice.

## 13.7 Moving your existing data

Stop the local terminal, then copy the two databases up and restart the service:
```powershell
scp $env:USERPROFILE\.abg-terminal\portfolio.sqlite3 $env:USERPROFILE\.abg-terminal\signals.sqlite3 ubuntu@<server-name>:~/abg-data/
```

Then, on the server:
```bash
sudo systemctl restart abg
```

## 13.8 Docker (alternative)

```bash
cp .env.example .env   # fill it in
docker compose up -d   # builds, runs on 127.0.0.1:8000, data in the abg-data volume, restarts automatically
sudo tailscale serve --bg 8000
```

## 13.9 Paper trading at a broker (optional)

1. Create a free Alpaca account and switch to **Paper Trading**. Generate API keys there.
2. In `.env` set `ABG_BROKER=alpaca_paper`, `ABG_ALPACA_KEY_ID=…`, `ABG_ALPACA_SECRET_KEY=…`.
3. Restart the service.

Entries, adds, stop moves, partial exits and exits for stock, ETF and crypto ideas are mirrored as
paper orders. Futures ideas stay tracked in the terminal only.

## 13.10 Troubleshooting

| Symptom | Check |
|---|---|
| dashboard unreachable | `systemctl status abg`; `tailscale serve status`; is your laptop on the tailnet? |
| service restarts in a loop | `journalctl -u abg -n 80 --no-pager` (usually a typo in `.env`) |
| "401 unauthorized" | sign in with the view or admin token (🔒 button) |
| no real-time prices | `ABG_FINNHUB_API_KEY` set? `/api/monitor` → `external.stream.connected` |
| slash commands missing | bot invited with the `applications.commands` scope; `/api/monitor` → `external.commands` |
| Oracle "out of capacity" | try another availability domain, retry off-peak, or use Pay-As-You-Go |

# 20. DAISY upgrade: MNQ bot, swing desk, pitch desk, new tabs

**Goal:** DAISY (this repo, `tolu-adeb/DAISY`) becomes the one app you run, and the single live Discord poster.
- The MNQ bot gets everything the spin-off (`c3.lab/mnq-bot`) added since Oct 22.
- The House of Traders **swing desk** (the stock tracker) runs inside the monitor.
- A new **pitch desk** produces researched stock pitches.
- The dashboard gets new tabs, and known terminal bugs are fixed.
- `c3.lab/mnq-bot` stays installed but held: it never posts unless `ABG_MNQ_PRIMARY=true`.

## How the port works

`mnq-bot` is the terminal's code with only import lines renamed (`PORTING.tsv`) plus 9 numbered patches
(`PATCHES.md`). So the port is mechanical:

1. **Changed files:** reverse the import renames and diff against DAISY. Only the bot, the lab core, the lab
   tools, the R&D routes and the R&D tab changed (patches 1, 5–9). Those files are taken over.
2. **New files** are copied with imports renamed back (`mnq.strategy` → `abg.alpha`, `mnq.lab` →
   `abg.agents.lab`, `mnq.swing` → `abg.swing`, `mnq.app.settings` → `abg.config`, ...).
3. **Spin-off-only parts are left out:**
   - the website publisher (`site.py`)
   - the shadow-week guard and the terminal-data importer
   - the research-papers website flow
   - the market-data shim; DAISY's own engine already provides those calls

## Stages

| # | Stage | What lands | Done when |
|---|---|---|---|
| 1 | Sync | DAISY fast-forwarded to the terminal's latest (`17e1b92`: Strategy Lab, R&D desk, Target 1 0.35R, duplicate-post fix); the Windows flaky-test fix committed | 247 tests pass |
| 2 | MNQ bot upgrades | duplicate-brief guard + late-brief hold, posting pause, day log + **live-vs-backtest check**, Sunday re-fit suggests instead of auto-adopting, lab fingerprint fixed on Windows, shared lab history, **forward lab** (walk-forward + paper testing of ideas), uptime + Windows keep-awake + autostart check + `health.json`, stale-lock fix (a closed window no longer blocks a restart) | the spin-off's lab / forward-lab / reliability tests pass against `abg` |
| 3 | Swing desk | `swingdesk/` package vendored unchanged, `abg/swing/` bridge (schedule, passes, intake, wide scan, routes, CLI `abg swing ...`), seed data, live state in `data/swing/` | the swing tests pass; seed replay reproduces the audit (63 taken, 32/40 wins, +14.25R) |
| 4 | Pitch desk v1 | `abg/pitch/`: universe + factor screen, data pack, valuation (DCF, reverse DCF, peer comps, bear/base/bull), memo with linked sources, optional Claude write-up from the data pack only, scorecard vs SPY, CLI `abg pitch ...`, weekly batch, optional `ABG_PITCH_WEBHOOK_URL` | memos for 3 tickers, every number traceable; tests on fixtures |
| 5 | Dashboard | new tabs: **MNQ Bot** (live plan, event log, paper stats, live vs backtest, uptime/health, pause), **Swing Desk** (book, add idea, all ideas, track record, wide scan), **Pitch Desk** (screen, memos, scorecard), **System** (monitor, jobs, errors, data freshness, settings check); R&D tab gains the forward lab | screenshots desktop + phone, no console errors |
| 6 | Fixes | everything in 2 + the gateway test race + any bug found while porting (logged in 20.2) | full suite green |
| 7 | Switch-over | data and `.env` keys carried over from the old working copy, autostart pointed at DAISY | DAISY's monitor holds the lock; mnq-bot held |
| 8 | Ship | docs, README, commit, push | pushed to `tolu-adeb/DAISY` |

## Rules kept

- **Secrets** stay in the local `.env` only.
- **Paper only;** nothing trades.
- **Loss controls** can't be loosened by any lab or agent.
- **Agents propose, code decides, you approve.**
- **One poster per Discord channel.**
- **New features stay off** until their `.env` switch is set.

## 20.1 Results

- **Stages 1-6 are in.** The full suite passes (see the commit for the count) and includes the spin-off's lab /
  forward-lab / reliability / swing / wide-scan tests plus 6 pitch-desk tests on fixtures.
- **Dashboard:** four new tabs (MNQ Bot, Swing Desk, Pitch Desk, System) and a Forward lab sub-tab under R&D, checked
  on desktop (1440 px) and phone (390 px) with no console errors.
  - The swing, wide-scan, forward-lab and live-vs-backtest screens are the spin-off's own code. Each file is wrapped so
    its names can't clash with the terminal's (`swing.js`, `wide.js`, `forward.js`, `parity.js`).
  - Their styles (`desk.css`) are scoped to the new tabs, so the old tabs look the same.
  - Sub-tabs have links: `#swing/add`, `#rd/forward`, `#pitch/memo`, ...
- **New `.env` switches,** all off by default: `ABG_SWING_ENABLED`, `ABG_FWD_ENABLED`, `ABG_PAPERS_ENABLED`,
  `ABG_PITCH_ENABLED` (with `ABG_PITCH_WEBHOOK_URL`, `ABG_PITCH_AI_MEMO`).
- **New commands:** `abg swing ...`, `abg pitch screen|make|list|score`.
- **New API:**
  - `/api/swing/*`, `/api/pitch/*`
  - `/api/alpha/pause|events|paper`
  - `/api/system`
  - `/api/rd/forward*`, `/api/rd/parity*`
- **Raw Discord export kept out of git:** `swingdesk/data/export.html` and `signals_raw.json` hold other people's
  messages and the repo is public. `signals.csv` (the tracked ideas) and the price cache are committed.

### Pitch desk v1

| Step | What it does |
|---|---|
| Screen | 50 large caps (or `ABG_PITCH_UNIVERSE`) plus your watchlist. Each factor is a z-score: 12-1 month momentum 30%, earnings yield 25%, profit margin 25%, expected EPS growth (forward vs trailing) 15%, low 3-month volatility 5%. A missing field drops that factor, not the name. |
| Valuation | 10-year earnings DCF. Growth fades from the first-year estimate to 2.5%. The discount rate is risk-free + beta x 5%. |
| Reverse DCF | The growth today's price implies. |
| Peer comps | Median P/E and forward P/E of same-sector names. |
| Bear / base / bull | Growth x0.5 / x1 / x1.5, rate +1% / 0 / -0.5%. |
| Fair value | Base DCF and comps, blended. |
| Verdict (rules, not a model) | **LONG** when fair value is 15%+ above the price *and* the name ranks in the top third. **WATCH** when only one holds. **PASS** otherwise. |
| Memo | Thesis, valuation table with every input, screen numbers, risks (generated from the data), sources: which provider served each piece, news links, SEC filings link. The Claude write-up is optional and only sees the data pack. |
| Scorecard | Every memo is marked against SPY from its pitch-day price. |

The free providers have no cash-flow statements, so the DCF discounts EPS (an owner-earnings proxy). Every memo says so.

## 20.3 Carrying your data over and switching

`scripts/carry_over.py` copies the old terminal's history into DAISY. The databases are copied with SQLite's backup
API, so a running monitor can't leave a half-written copy.

**What it copies:**
- From the old terminal: bot trades and events, portfolio, signals, R&D digests, Strategy Lab, backups, Alerio, models,
  bar caches.
- From the spin-off: the swing desk's book, prices and wide scan; the forward lab; day logs; the live-vs-backtest check;
  research papers.

**`.env`:** it builds DAISY's `.env` from the terminal's, adds the spin-off keys DAISY understands and switches the new
desks on. Values are never printed.

**One poster per channel.** Until you switch, every Discord webhook and e-mail line in DAISY's `.env` is commented out
with `#HELD`, so a DAISY window opened early shows everything on the dashboard and posts nothing.

```bat
cd %USERPROFILE%\Documents\DAISY
python scripts\carry_over.py --terminal ..\school\AIBG\abg-terminal --mnq ..\c3.lab\mnq-bot
```

**After the close, to switch:**
1. Close the old terminal's monitor window (or stop its task).
2. Re-run the copy with `--switch`. It refuses while the old monitor's heartbeat is fresh, then copies once more and
   releases the webhooks.
3. Start `Start Monitor.bat` in DAISY. The first start builds DAISY's own `.venv`.
4. Point the "ABG Monitor" autostart task at DAISY: `powershell -File deploy\windows-autostart.ps1` from the DAISY
   folder.

`c3.lab/mnq-bot` stays installed and held.

## 20.2 Bugs fixed

- Morning brief posted more than once after a restart: `delivered_before` guard + late-brief hold (`ABG_ALPHA_BRIEF_UNTIL`).
- A closed window left a stale monitor lock: dead same-host pids are now detected (Windows-safe, no `os.kill`).
- A second dashboard window stayed read-only forever: it now retries and takes over when the other monitor stops.
- Sunday re-fit wrote new bot settings by itself: it now only suggests them to the forward lab (`ABG_ALPHA_REFIT_ADOPT`).
- Lab fingerprints differed on Windows (path separators), so lab history didn't match: fixed.
- `/track` test raced on Windows (fixed sleep): it now waits for the save.

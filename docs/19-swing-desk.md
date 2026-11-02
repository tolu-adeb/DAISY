# 19. Swing desk

The House of Traders swing desk (the Oct 22, 2026 audit: *Swing Signals — Audit & Alpha Strategy v1*) runs inside
the MNQ Bot's monitor and has its own section on the dashboard.  Paper only: it grades, tracks and alerts; it
never places an order.

## 19.1 What's where

| Path | What | In git |
|---|---|---|
| `swingdesk/` | The desk project, copied unchanged: `swingdesk/` (code), `config/strategy.yaml` (the rules), `config/universe.txt` (scanner list), `analysis/` (the audit's scripts and results), `data/` (the 88 audited ideas and the price cache through Oct 21; the raw Discord channel export stays on your PC, outside git, because the repo is public), `tests/` (its 11 tests), `.github/workflows/` (inert here: GitHub only runs workflows at the repo root) | yes |
| `abg/swing/` | The bridge: `desk.py` (schedule, passes, Discord, views), `intake.py` (pasted text → plan), `routes.py` (API), `cli.py` (`abg swing ...`) (the parser and instruments come straight from `abg/extsignals` and `abg/markets`) | yes |
| `data/swing/` | Live state: `data/signals.csv` (the 88 + everything you add), `data/scanner_signals.csv`, `data/prices/` (refreshed daily bars), `state/book.json` (the replayed book), `state/alerts.jsonl` (every alert and where it went), `state/desk.json` (schedule), `research/` (proposals and R&D reports) | no |

The first start copies the seed signal list and price cache from `swingdesk/data/` into `data/swing/` (never
overwriting).  Nothing at runtime writes into `swingdesk/`, so `git pull` never conflicts with your book.

## 19.2 Schedule (inside `abg serve` / `abg monitor`)

| When (ET) | What |
|---|---|
| First start | **Seed**: grade and replay all 88 ideas silently. History is logged as "history (not posted)" and never sent to Discord. |
| Trading days 9:30–16:00, every `ABG_SWING_INTERVAL_MIN` (30) | Refresh daily bars for open ideas + SPY, live quotes for working orders, replay, alert on changes (graded, in zone, filled, target, stop, cancelled, expired). |
| Trading days `ABG_SWING_CLOSE_TIME` (16:20) | Scanner over `config/universe.txt` (if `ABG_SWING_SCAN`), final pass, daily digest; Fridays also the weekly digest. |
| Sundays 11:00 | R&D agent, only with `ABG_SWING_RESEARCH=weekly` and an Anthropic key. It writes a proposal; the rules change only when you edit and commit `config/strategy.yaml`. |

The replay is the source of truth (`swingdesk.monitor.update`, unchanged), so a restart or a second pass never
repeats an alert.  Prices come from yfinance by default; `ABG_SWING_USE_TWELVEDATA=true` switches to Twelve Data
(it shares the MNQ bot's quota, so it's off by default).

## 19.3 Discord

`ABG_SWING_WEBHOOK_URL` = the channel for swing alerts.  Unset, alerts stay on the dashboard.  Each pass posts at
most `ABG_SWING_MAX_POSTS` (12) and summarises the rest in one message.  The scanner's skipped setups stay on the
dashboard.  The MNQ shadow-mode guard doesn't apply here (the terminal doesn't run this desk), so set the webhook
only when you want the alerts to go out.

## 19.4 Dashboard: Swing desk section

* **Book**: the market gate (SPY vs its 20-day average and what that means for new ideas), the scoreboard, open
  positions and working orders as price ladders (stop, entry zone, limit, take-profit, fill, latest price), the
  live alert feed, and *Refresh prices now*.  Click any row for its plan, size, gate reasons and timeline; working
  orders can be cancelled there.
* **Add idea**: paste a post; it's read by the channel format, then the terminal's parser, then (prose, with a
  key) the intake agent.  The levels land in an editable form and the desk grades them as you type: take or skip,
  tier, limit, stop, 0.75R take-profit, risk and shares (enter your account size once; it's kept in this browser).
  *Track it* adds it with the current time as the post time.  A missing stop or target is never invented.
* **All ideas**: every idea with filters (open, closed, skipped, never filled), search, sparkline.
* **Track record**: cumulative R, average win and loss, the break-even win rate, and win rate by author, tier
  and month.
* **Research**: the rules in plain English (and the file), the audit's variants replayed on your current data
  (all / Jul–Aug / Sep–Oct), digest buttons, the R&D agent, proposals and reports.

## 19.5 Command line

```
abg swing status                 scoreboard, gate, open positions and working orders
abg swing pass [--offline]       run a pass now
abg swing add "TEXT" [--author]  parse, show the verdict, then track it (asks first)
abg swing scan | digest [--period weekly] [--post] | research [--deep]
pytest swingdesk/tests           the desk's own tests (run from swingdesk/), plus tests/test_swing.py
```

## 19.6 Settings (`.env`, all optional)

`ABG_SWING_ENABLED` (true), `ABG_SWING_WEBHOOK_URL`, `ABG_SWING_INTERVAL_MIN` (30), `ABG_SWING_CLOSE_TIME` (16:20),
`ABG_SWING_SCAN` (true), `ABG_SWING_RESEARCH` (off | weekly), `ABG_SWING_PITCH` (false; the analyst agent isn't
wired into the monitor yet), `ABG_SWING_AI_INTAKE` (true), `ABG_SWING_USE_TWELVEDATA` (false),
`ABG_SWING_MAX_POSTS` (12).

## 19.7 Checks (Oct 22, 2026)

The seed replay on the cached prices reproduces the audit exactly: 63 taken, 40 closed, 32 wins (80.0%), +14.25R,
7 open (PNTG, COIN, BITX, BXSL, AMD, MET, LLY), 4 working (CTSH, BRK.B, KO, MA), 25 skipped; SPY 773.09 vs
770.88, so the market branch is off.  The variants table reproduces the audit's variant rows.
`tests/test_swing.py` pins these.

## 19.8 Wide scan: every liquid US stock, ranked

The core scanner checks the 85 names in `config/universe.txt`.  The wide scan runs the same setup over the whole
market every trading evening and ranks what it finds.  Code: `abg/swing/wide.py` (method, backtest) and
`abg/swing/wide_desk.py` (the nightly job, delivery, the dashboard view).

| Step | What | Source |
|---|---|---|
| Market | Every US stock's daily bar, one call per session; kept in `data/swing/wide/bars/` | Polygon grouped daily |
| Universe | US common stocks (refreshed weekly), minus the core 85; last close ≥ $5, median 20-day dollar volume ≥ $20M, ≥ 60 sessions | Polygon reference tickers |
| Setup | `swingdesk.scanner.find_setups` on the latest session (unchanged) | |
| Gate | `swingdesk.strategy.grade` (unchanged); earnings inside the next 10 sessions add `binary_event` = half size | Finnhub earnings calendar |
| Rank | 0–100: 20-day and 60-day return vs SPY (35% / 20%), the sector ETF vs SPY (15%), volume vs its 20-day average (10%), headlines (20%); earnings in the window subtract | Finnhub sector + headlines; Claude Haiku reads the headlines for the top 12 |
| Market | Breadth: share of the liquid universe above its 50-day average.  Under `ABG_SWING_WIDE_MIN_BREADTH` (30%) nothing posts | computed |
| Track | Every taken setup goes to `data/swing/data/wide_signals.csv` (author `wide`) and is replayed by the normal pass on end-of-day bars | |
| Post | Shadow by default (dashboard only).  With `ABG_SWING_WIDE_POST=true`: the best `ABG_SWING_WIDE_MAX_POSTS` (3) without red flags go to the swing webhook, and only those ideas' fills and exits follow | |

Red flags (Claude, or a keyword check without a key): share offering, lawsuit or investigation, downgrade, guidance
cut, going concern / bankruptcy, regulatory setback, CEO/CFO exit.  A flagged idea is still tracked, never posted.

Wide ideas stay out of the Book, the main scoreboard, the Track record and the digests: they have their own page
(**Swing desk → Wide scan**) with the ranked list, the shadow book and record, and the backtest.

Schedule: trading days at `ABG_SWING_WIDE_TIME` (17:15 ET), retried every 30 minutes until 21:30 if Polygon hasn't
published the session yet.  The first run downloads `ABG_SWING_WIDE_HISTORY` (130) sessions at 5 calls a minute
(free plan): about half an hour, in the background.

```
abg swing wide check              try each source once (no keys printed)
abg swing wide backfill           download the missing sessions of whole-market bars
abg swing wide run [--post]       run the scan now and print the ranked list
abg swing wide backtest           replay every liquid stock since 2026-07-01 (no news: it can't be replayed)
```

Keys (`.env` only): `ABG_POLYGON_API_KEY` (required), `ABG_FINNHUB_API_KEY` (sectors, earnings, headlines),
`ABG_ANTHROPIC_API_KEY` (headline reading).  FMP, Tiingo and Twelve Data aren't used: Polygon and Finnhub cover the
same data, and Twelve Data's quota is shared with the MNQ bot.

Before turning posting on: the core scanner's own record is thin (Jul–Oct, 85 names: 19 taken, 3 filled, 3 wins),
so the 80% win rate belongs to the channel's ideas, not to the scanner.  Run the backtest, then let the shadow record
build for 2–4 weeks and compare it with the channel before setting `ABG_SWING_WIDE_POST=true`.

## 19.9 Wide scan: what the backtest changed (Oct 23, 2026)

The first backtest (Jul 1 – Oct 22) showed the ranking picked the wrong ideas: the **top 3 a day by score** (what
posts) went 57% and −3.3R over 21 closed trades, and the top-scored third was the worst bucket.  80% of setups never
filled: strong stocks don't pull back into the zone.  Re-run from Jun 8 (271 taken setups, 48 closed):

| | Closed | Win rate | Total R |
|---|---|---|---|
| Top 3 a day by score, limit only (before) | 25 | 52% | −6.4R |
| Top 3 a day nearest to entry, limit only | 33 | 67% | +1.4R |
| **Top 3 a day nearest to entry, chase up to 1 ATR (now)** | **45** | **76%** | **+6.6R** |
| Chased entries only | 35 | 83% | |

Changes (wide ideas only; the channel's ideas are untouched):

* **Ranking** (`ABG_SWING_WIDE_RANK=entry`): taken setups post nearest-to-entry first (last close to the order in
  daily ATRs), i.e. the ones likeliest to fill.  `score` brings back the old order.
* **Chase** (`ABG_SWING_WIDE_CHASE_ATR=1.0`): if a session closes above the zone (long) by at most 1 ATR before the
  zone is touched, and the target is still more than half the distance away, buy the next open with the same stop.
  On the channel's 64 taken ideas the same rule made no difference (−0.2R), so it applies only to the wide scan.
  Engine: `swingdesk/engine.py` (`Plan.chase_atr`, PATCHES #7); the R&D variants table also shows "chase up to 1 ATR".
* **Posting gate**: with `ABG_SWING_WIDE_POST=true` the scan posts only when the latest backtest of exactly what
  would post (same ranking and chase) has, **outside 2026-06-08 to 2026-10-22** (the window those two choices were
  made on), ≥ 40 closed trades (`ABG_SWING_WIDE_POST_MIN_TRADES`), ≥ 65% wins (`ABG_SWING_WIDE_POST_MIN_WIN`) and a
  positive total R.  Otherwise ideas are tracked and marked "would post (held)", with the reason on the page.
* **More history**: each night after the scan, up to 60 older sessions (`ABG_SWING_WIDE_BACKFILL_PER_NIGHT`) toward
  400 (`ABG_SWING_WIDE_BACKFILL_TO`, ~100 MB, Polygon's free plan covers two years).  The backtest re-runs on all
  stored history on Saturdays at 09:00 ET (`ABG_SWING_WIDE_BACKTEST_WEEKLY`), so the gate gets out-of-sample trades
  within about two weeks.

These numbers are small samples chosen on the data they are reported on; that's why the gate counts only other data.

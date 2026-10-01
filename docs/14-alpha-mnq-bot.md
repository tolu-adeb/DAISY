# 14. MNQ signal bot (alpha, test mode)

A rule-based, self-adjusting opening-session strategy for Micro Nasdaq-100 futures (MNQ). It posts
the trade lifecycle to its own Discord channel, in the same style as the signal services you follow:
pre-market brief → trade idea → signal validated → Target 1 → runner → final, stop or break-even →
day closed. Every message says why. The backtester runs the exact code the live bot runs.

> **Status: TEST.** Every message is tagged `TEST · paper signals`. The one setup the data supports
> was found and checked on only ~42 sessions of 5-minute bars (Aug 3 – Oct 1, 2026) - about 30
> trades. That is a hypothesis worth paper-trading, not proof. Don't point a copy-trader (Alerio) at
> this channel until weeks of paper results hold up.

## 14.1 What the evidence says (and why the rules look like this)

Two datasets were available when it was built.

**MNQ daily bars (Jul 7 – Oct 1, 2026, investing.com).**
- The September roll was spliced badly. Investing kept quoting the expiring September contract
  through Sep 14–18 while volume had already moved to December, then jumped to December on Sep 21.
- `abg alpha daily` detects this: low volume in the expiry week drops those 5 rows, and earlier
  prices are shifted up by the carry offset (+243.75 pts). The offset agrees with the signal
  service's own Sep 15 entries, which sit at least 243 pts above the stale September high.
- On the cleaned series, the daily "trend" call was right on 44% of 52 days, and "same as yesterday"
  on 56%. Neither is better than a coin flip. So the day's bias is only a soft lean (±5 score
  points); it never blocks a setup.

**TradingMind's published journal: 108 trades.** Their own paper numbers: +2,804 pts, PF 2.5,
max drawdown 303 pts. Sliced by time and sequence:

| Slice | Trades | Win | Avg pts |
|---|---|---|---|
| Entries before 10:00 | 42 | 67% | +50.2 |
| 10:00–10:30 | 33 | 55% | +21.2 |
| 10:30–11:30 | 28 | 32% | **−8.2** |
| First trade of the day | 55 | 64% | +39.4 |
| Any trade after a loss that day | 22 | 23% | **−2.5** |

The rules were then fitted on Jul–Aug and checked on September:

| Rule | Jul–Aug: net / max DD / PF | Sep (unseen): net / max DD / PF |
|---|---|---|
| as published | +2,117 / 303 / 2.48 | +687 / 103 / 2.65 |
| stop after the first loss + no entries 10:30–11:30 | +2,131 / **89** / **4.50** | +788 / **57** / **4.10** |

Same profit, less than a third of the drawdown. These rules are built into the bot as defaults:

- entries 09:35–10:30
- stop after the first loss
- at most 2 trades a day

You can also replay any service's DiscordKit export through `abg alpha journal` to check this on
its own record.

## 14.1b What the intraday data says (MNQ 5-min, Aug 3 – Oct 1, 2026)

The first version ran the three setups in the table further down. On real bars it lost: 25 trades, 52% win,
−76 pts, profit factor 0.91. The sweep setup did most of the damage (22 trades, −138 pts).

Looking at the fit window only (Aug 3 – Sep 10):

- **The opening move mostly reversed.** The 09:30–09:45 drive carried on to 10:30 only 39% of the time.
- **Breakouts failed on average.** Taking the first 5-min close outside the 15-min opening range lost
  28 pts an hour later.
- **But it came in streaks.** Some weeks breakouts ran, other weeks they reversed. Always fading the
  first breakout made +0.18R a trade, and always following it lost −0.16R.

**Adaptive rule:** each morning, replay the last 3 sessions both ways, then trade today in the mode
that earned more, or stand aside if neither did.

| Version (stop 0.8 × opening ATR, half at 1R, final 2R) | Trades | Avg R | Net pts | PF | Max DD |
|---|---|---|---|---|---|
| Whole sample, engine with costs | 31 | **+0.36** | **+664** | 2.25 | 184 pts |
| Sep 11 – Oct 1 only | 12 | +0.23 | +103 | 1.44 | 84 pts |
| Walk-forward: settings fitted on Aug 3 – Sep 10, tested Sep 11 – Oct 1 | 9 | +0.28 | +101 | 1.53 | 102 pts |

**Robustness.**
- The result stayed positive across most nearby variants: opening range 5 or 15 min, cut-off
  10:00–11:00, final target 1.5–3R.
- It faded with a 30-minute range.
- On random-walk data the same rule shows −0.06R over 294 trades, so the mode choice isn't
  manufacturing an edge.

**The caveat.** The rule was chosen after looking at these 42 days. With about 30 trades, +0.36R is
roughly two standard errors from zero. Treat it as promising, not proven.

## 14.2 The strategy

**Default setup - `orx`, the adaptive opening-range trade.**
- **Mode.** Chosen before the open, as above: follow, fade or off. It is shown in the 09:25 brief with
  the last 3 sessions' numbers.
- **Entry.** The first 5-min close outside the 09:30–09:45 range, before 10:30. Take it in the
  day's mode.
- **Exits.** Stop at 0.8 × the average 5-min range of the opening 15 minutes. Target 1 at **0.7R**
  banks half and moves the stop to break-even. Final target at 2R.
- **Frequency.** At most one trade a day.

**Win rate vs profit.** Where Target 1 sits mostly decides the win rate. Once half is banked and the
stop is at break-even, the trade can't lose, so an earlier Target 1 turns more trades into wins.
Results on the same bars:

| Target 1 | Trades | Win rate | Avg R | Net pts | PF | Win rate Sep 11 – Oct 1 | Net Sep 11 – Oct 1 |
|---|---|---|---|---|---|---|---|
| 1.0R | 31 | 68% | +0.36 | +664 | 2.25 | 58% | +103 |
| **0.7R (default)** | 34 | **74%** | +0.33 | +677 | 2.35 | 62% | +31 |
| 0.5R | 33 | 79% | +0.30 | +582 | 2.42 | 67% | n/a |

- Across the whole sample, the higher win rate cost little.
- In the September window it cost profit: +31 pts against +103.
- To favour profit over win rate, set `"t1_r": 1.0` in `alpha_params.json`.
- The Sunday re-fit only considers settings with at least `ABG_ALPHA_MIN_WIN_RATE` (default 65%)
  wins on the fit window.

The three original setups below are still in the code. They are off by default (`setups` in
`alpha_params.json`) because they lost on real bars.


All times are ET. The base bars are 1-minute (5-minute works). Setup checks run on 5-minute closes.

| Setup | Trigger | Entry | Stop |
|---|---|---|---|
| **ORB retest** | A 5-min close outside the 09:30–09:45 range, with a body ≥ 0.6×ATR5 that closes in the top or bottom 40% of the candle, arms a **trade idea** | Price comes back to the range edge and a bar closes back in the breakout direction | 0.9×ATR5 beyond the zone |
| **Sweep & reclaim** | Price runs a key level (PDH, PDL, ONH, ONL, ORH, ORL) by 0.12–1.2×ATR5, then a 5-min candle closes back across it with displacement | That close (direct signal) | Beyond the sweep extreme plus a buffer |
| **VWAP pullback** | One-sided session: ≥ 80% of bars on one side of VWAP, efficiency ≥ 0.3, VWAP rising or falling. First pullback to VWAP that closes back with the trend | That close | Below or above the pullback swing |

**Targets and management**
- Target 1 = `t1_r` (0.7R by default). Half is banked and the stop moves to break-even.
- The runner trails by 1.5×ATR5 once it is +1.5R.
- The final target is the next liquidity level at least 1.8R away (PDH/PDL/ONH/ONL/OR/VWAP), capped at 4R. If there is none, it is 2.5R.
- Stops are bounded to 12–90 pts.

**Filters**
- No entries from 5 min before to 10 min after a high-impact release. Releases come from the calendar; add 10:00 releases to `calendar.json`.
- FOMC day: half size by default (`fomc_mode` in the params file: half, skip or normal).
- A choppy open (≥ 4 VWAP crosses) costs 15 score points.
- One idea per zone per day, so no re-firing. TradingMind had this exact bug on Jul 20.
- The ORB entry isn't chased more than 0.35R past the zone.

**Scoring** starts at 50.
- +10 for a key level, +10 before 10:00, ±5/8 for the bias, +5 for ≥ 2.5R room, −15 for chop.
- The adaptive book adjusts it by up to ±12.
- A signal needs 55.

## 14.3 How it keeps up with the market

1. **Every day.** Before the open it replays the last 3 sessions, following and fading the first
   breakout, and picks today's mode: follow, fade or stand aside. This is the main adaptive piece. It
   also rebuilds the levels, daily ATR, lean and the day's macro events. The brief at 09:25 says which
   mode and why.
2. **Every bar.** Opening range, VWAP, ATR5, session efficiency and VWAP crosses are recomputed. Each
   trade is labelled `trend`, `range` or `news` at entry.
3. **Every trade.** The adaptive book (`alpha_learner.json`) keeps a recency-weighted, shrunk
   expectancy per setup and condition, and adjusts the score. A setup is paused in a condition after
   at least 6 trades below −0.15R, and switches back on by itself when results recover.
4. **Every Sunday 19:00.** A walk-forward re-fit runs on the last ~60 days of 5-min bars. It covers
   the stop multiple, the final R and the replay window. New settings are adopted only if they are
   profitable out of sample after costs, on 15 or more trades, and beat the current ones.

## 14.4 Backtest it (run these on your PC — it can reach Yahoo)

```powershell
abg alpha sanity                                       # integrity: no edge on random-walk bars
abg alpha backtest --walk-forward                      # MNQ=F 5-min, last ~60 days, fit 2/3 test 1/3
abg alpha backtest --interval 1m --period 7d           # 1-minute bars, last 7 days
abg alpha backtest --csv my_mnq_1min.csv --trades      # your own export (TradingView / NinjaTrader)
abg alpha backtest --walk-forward --save --seed-learner   # adopt settings + warm up the adaptive book
abg alpha replay --csv my_mnq_1min.csv --date 2026-09-23  # the exact messages it would have sent that day
abg alpha journal bot-journal_TradingMind.json         # test day rules on a service's journal export
abg alpha daily MNQ_daily.csv --journal trades.csv     # roll fix + daily-bias check
```

How the backtest works:
- **Fills.** Entries fill at the signal close plus 1 tick. Stops fill at the stop minus 1 tick, and if a bar touches both the stop and a target, the stop is assumed to come first.
- **Costs.** Commission is $1.24 per micro round turn (`ABG_ALPHA_COMMISSION_RT`).
- **Integrity.** On structureless random-walk data the strategy shows no edge: +0.035R average over 284 trades, which is noise. So the simulator isn't leaking future information.
- **What to trust.** Trust the out-of-sample numbers from `--walk-forward`, and only once there are 30+ trades.

## 14.5 Turn it on

Add these lines to `.env` (on the server: `~/.env.server.local`, then rerun setup):

```
ABG_ALPHA_ENABLED=true
ABG_FINNHUB_API_KEY=...            # real-time QQQ stream drives the live bars
ABG_ALPHA_WEBHOOK_URL=https://discord.com/api/webhooks/...   # a NEW channel, e.g. #abg-mnq-test
# or bot mode (threads replies under the signal, edits cancelled ideas):
# ABG_DISCORD_BOT_TOKEN=... and ABG_ALPHA_CHANNEL_ID=123...
# ABG_ALPHA_MENTION_ROLE_ID=123... # optional role ping on signal / T1 / stop / final
ABG_ALPHA_TAG=TEST
```

**Data.** Free CME futures data on Yahoo is about 10 minutes late, which is too late for entries.
The bot builds its bars from **real-time QQQ trades** (Finnhub websocket) and converts them to MNQ
points with a ratio. The ratio is calibrated from same-minute closes of both (yesterday's 15:59)
and re-checked every 30 minutes; the basis drifts well under a point a day. Levels and the ATR
warm-up come from the MNQ bars themselves. If the stream goes quiet, the bot falls back to polling
MNQ bars and logs that they are delayed. With a real-time futures feed, set `ABG_ALPHA_FEED=direct`.

**Where to watch it.** The Markets tab has an MNQ panel: today's levels, the open trade, recent
trades, the backtest summary and the adaptive book. `abg alpha status` shows the same in a terminal.

## 14.6 Settings

| Variable | Default | |
|---|---|---|
| `ABG_ALPHA_ENABLED` | false | run inside the monitor (`abg serve` / `abg monitor`) |
| `ABG_ALPHA_SYMBOL` | MNQ=F | futures bars for levels / direct feed |
| `ABG_ALPHA_FEED` | proxy | proxy (real-time QQQ × ratio) or direct |
| `ABG_ALPHA_PROXY_SYMBOL` | QQQ | |
| `ABG_ALPHA_WEBHOOK_URL` / `ABG_ALPHA_CHANNEL_ID` | — | destination (webhook, or bot token + channel) |
| `ABG_ALPHA_MENTION_ROLE_ID` | — | role to ping |
| `ABG_ALPHA_TAG` | TEST | prefix on every message |
| `ABG_ALPHA_CONTRACTS` | 1 | micros, for $ figures |
| `ABG_ALPHA_COMMISSION_RT` | 1.24 | $ per micro round turn in backtests |
| `ABG_ALPHA_POST_IDEAS` / `ABG_ALPHA_POST_BRIEF` | true | |
| `ABG_ALPHA_AUTO_OPTIMIZE` | true | Sunday walk-forward re-fit |

Strategy parameters (window, max trades, break-even style, targets, stops, score threshold) live in
`ABG_DATA_DIR/alpha_params.json`. `--save` and the Sunday job write that file. You can also edit it
yourself; the field names are the `AlphaParams` fields in `abg/alpha/strategy.py`.

## 14.7 Limits

- These are paper signals. Real fills on a fast open can be worse than a 1-tick slip.
- The proxy feed can differ from MNQ by a couple of points.
- 60 days of 5-minute bars is about 100 trades: enough to reject a bad idea, not enough to prove a good one.
- Educational only, not financial advice.

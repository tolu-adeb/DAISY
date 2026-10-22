# MNQ Opening-Session Bot: Research and Insights Log

Source project: ABG Intelligence Terminal v3, module `abg/alpha/` (Python), repo `tolu-adeb/DAISY`.
Covers: the bot's first build (Oct 1, 2026), the loss-cap work, and the 2-year re-test and redesign (Oct 21–22, 2026).
Instrument: Micro Nasdaq-100 futures (MNQ, $2 per point). All times are US Eastern.
Status: paper / TEST mode only. Nothing here has been confirmed with live fills.

---

## 0. How to use this file (for an agent)

- Treat every number as a **backtest result on proxy data**. Re-verify in your own project before relying on it (Section 9 gives the protocol).
- Section 2 is the strategy spec. It is complete enough to re-implement from.
- Section 3 lists what helped and what didn't, with sizes. Use it to skip dead ends.
- Section 6 lists the bugs and traps that cost the most time. Check your implementation against each one.
- Hard constraints carried over from the owner (keep them in any port):
  - API keys (Finnhub, Twelve Data, Tiingo, etc.) live only in a local `.env`. Never commit them, never print them, never put them in archives, docs, or memory.
  - Discord: bot tokens or webhooks only. No self-bots.
  - Stay in paper/TEST mode until weeks of live paper results hold up. Live brokerage only if explicitly enabled.
  - Do not point a copy-trader (Alerio, which copies signals to the owner's Lucid funded account) at this bot until paper results hold up.

---

## 1. TL;DR

1. **The core edge:** trade the *first 5-minute close outside the 15-minute opening range* (9:30–9:45), **on the side of the 50-day daily trend.** If the break goes with the trend, follow it. If it goes against the trend, fade it. Don't try to pick follow vs fade from recent days.
2. **Over 577 sessions (Jul 2024 – Oct 2026)** with full trend history, the live configuration made:
   - 566 trades at a 66% win rate, +0.12R per trade, profit factor 1.36
   - +$5,478 per micro after costs, max drawdown $584
   - 21 of 28 months positive, every half-year positive, about 20 trades a month
   - Walk-forward out of sample: +0.11R, +$4,047 per micro
3. **The old rule** (pick follow or fade from the last 3 sessions' replays) was about break-even over the same 2 years: 387 trades, +0.03R, 2 of 5 half-years positive. It looked great on the 42 sessions it was designed on (+0.36R). **The classic overfitting story: short samples flatter you.**
4. **The largest real-world problem wasn't strategy, it was uptime.** The live bot was running at the 9:30 open on only about 5 of 15 sessions. A missed open means no trade.
5. **Two of the most valuable fixes were bugs:**
   - An adaptive filter that switched setups off could never switch them back on.
   - A daily-history filter could leak the future trend into every day.
6. **Win rate is mostly set by where Target 1 sits**, not by entry skill. A 0.7R Target 1 gives about 66% wins; 1R gives more profit at about 58% wins.

---

## 2. Strategy specification (current defaults)

### 2.1 Data and bars
- **Signals run on 5-minute bars.** 1-minute bars are fine as input; they're aggregated.
- **Live price source is a proxy:** real-time QQQ trades (Finnhub websocket) × a ratio of about 41.25 to get MNQ points, rounded to 0.25-point ticks. Free CME futures data (Yahoo) runs about 10 minutes late, which is too late for entries.
  - The ratio is calibrated from same-minute closes of both instruments and re-checked every 30 minutes. The basis drifts well under a point a day.
  - Validated on 43 overlapping days: the proxy reproduced all 26 real-MNQ trades, on the same side.
- **Daily trend input:** a daily close series (MNQ, or QQQ × ratio) with at least 50 days of history **strictly before** the trading day.

### 2.2 Context, built before the open each day
- `trend_long` (the trade side) = sign(last close − 50-day SMA of closes).
- `trend_mid` = the same with a 20-day SMA; `trend_d` = 10-day. Used only for the grade.
- **Grade:** A when the 10-, 20- and 50-day trends all agree, else B. Informational only, not a filter.
- **Levels:**
  - PDH / PDL / PDC: prior day high, low and close
  - ONH / ONL: overnight high and low (18:00–9:30)
  - ORH / ORL: opening range high and low (9:30–9:45)
  - VWAP, plus ATR5 (14-period ATR on 5-minute bars)
- **Regime label**, recorded at entry: `trend`, `range` or `news`. Used by the adaptive book.
- If there is no trend read (fewer than 50 prior closes), the opening-range trade stands aside.

### 2.3 Setup 1: `orx`, the opening-range trend trade (about 97% of trades)
- **Opening range:** high and low of 9:30–9:45. "Opening ATR" = the average high-low of the 5-minute bars inside the range.
- **Trigger:** the first 5-minute close outside the range. In practice it lands in the 9:50–10:10 bars.
- **Side:** always `trend_long`. If the break is in the trend's direction, it's a follow; if against, a fade (enter opposite the break).
- **Entry:** at the trigger bar's close plus 1 tick of slippage.
- **Stop:** entry ∓ 0.8 × opening ATR, bounded to 12–55 points.
  - If the structural stop is wider than 55 points, it's pulled in to 55 (`risk_cap_mode="tighten"`). No single trade can lose more than about $110 per micro.
- **Targets:** see Management.
- At most one `orx` trade a day.

### 2.4 Setup 2: `orb`, breakout-retest (with the trend only, about 3% of trades)
- **Arms an idea when:** a 5-minute close lands outside the range with a body at least 0.6 × ATR5, and the close sits in the outer 40% of the candle.
- **Entry:** price returns to the range-edge zone (±0.25 × ATR5) and a bar closes back in the breakout direction.
  - No chasing more than 0.35R past the zone.
  - The idea expires after 45 minutes.
- **Stop:** 0.9 × ATR5 beyond the zone, with a 0.15 × ATR5 buffer.
- **Only taken in the daily-trend direction** (`trend_filter=True`).

### 2.5 Management (all setups)
- **Target 1 at 0.7R:** bank half and move the stop to break-even.
- **Runner:** trails 1.5 × ATR5 once it is +1.5R.
- **Final target (liquidity mode):** the nearest prior-day or overnight level in the trade's direction that sits 1.8–4R away. If there is none, 2R for `orx` (2.5R for other setups). Members see a named target, e.g. "PDH 20,412".
- **No-follow-through cut:** after 20 minutes, if the best move is still under 0.35R and price is under water, exit.
- **Flat by 12:00.** No new entries after 11:30 (entry window 9:35–11:30).

### 2.6 Day-level rules
- At most 2 trades a day. Stop after the first loss. Daily loss cap of 1.5R.
- **News blackout:** no entries from 5 minutes before to 10 minutes after a high-impact release.
- **FOMC day:** half size.
- **Score:** starts at 50; a signal needs at least 55.
  - +10 at a key level
  - +10 before 10:00
  - ±5 to 8 for the day's lean
  - +5 for at least 2.5R of room
  - −15 for a choppy open (4 or more VWAP crosses)
  - ±12 from the adaptive book
- **One idea per zone per day**, so ideas never re-fire. One signal service had this exact bug.

### 2.7 Self-adjustment
1. **Adaptive book**, updated after every trade. For each (setup, regime) pair it keeps an exponentially weighted average of realized R:
   - Half-life 25 trades; each R is clipped to [−3, +5].
   - The average is shrunk toward a prior of +0.05R weighted as 8 trades: `(ew + 0.05×8) / (w + 8)`.
   - It moves the score by `clip(25 × m, ±12)`.
   - It switches a setup off in a regime once there are at least 6 trades and the shrunk average is below −0.15R.
   - **Blocked trades are still followed on paper and recorded, so a setup can switch back on.** Without this, the book locks itself out permanently (Section 6.1).
2. **Sunday re-fit**, every Sunday at 19:00 ET:
   - A walk-forward grid search on about 60 days of 5-minute bars, over the trend length (20/50/100), stop multiple and final R.
   - New settings are adopted only if, out of sample after costs, they have at least 15 trades, average more than +0.05R, are net positive, and beat the current settings.
   - Loss-control keys can't be loosened by a saved or re-fit file unless it says `"safety_override": true`.
   - A settings file without `orx_policy` (written under the old rule) is ignored entirely.
   - **Known weakness:** 60 days is a small sample. Better: feed it the long multi-year history.
3. **What it does not do:** invent new rules. Rule changes come from offline research like this log.

### 2.8 Live safety plumbing
- **Stale guard:** never post a trade message whose bar is older than 120 seconds. Replays after a restart stay off Discord.
- **Restart lock:** a restart keeps the day's earlier decisions instead of recomputing them.
- **Feed fallback:** if the websocket has been quiet for more than 30 seconds, poll real-time proxy quotes (rejecting quotes older than 90 seconds). Only if that also fails for more than 180 seconds, fall back to delayed bars, and log it.
- Every message carries `TEST · paper signals`, and every message says why.
- **Message lifecycle:** brief at 9:25 → idea → signal validated → Target 1 → runner updates → final, stop or break-even → day closed.

---

## 3. What helped and what didn't

All results are from the same simulator on 577 sessions of 5-minute proxy bars, with costs of $1.24 per micro round turn, entry at the signal close +1 tick, stop at −1 tick, and stop-first when a bar touches both stop and target. "R" means multiples of the initial risk.

### 3.1 Adopted, ranked by impact

| # | Change | Effect | Why it works (best explanation) |
|---|---|---|---|
| 1 | **Trend rule replaces adaptive follow/fade** (`orx_policy="trend"`) | +0.03R → +0.11–0.15R; drawdown $1,849 → $584–881; half-years positive 2/5 → 5/5 | The 3-session pick was no better than always following (both about +0.03R). The trend side carries the information: breaks *with* the 50-day trend continue, and breaks *against* it fail. Both legs pay (follow +0.13R, fade +0.11R). |
| 2 | **Paper-tracking blocked trades** in the adaptive book | 14 trades over 2 years → 566; −$572 → +$5,478 | One bad run in mid-2024 switched `orx` off, and because a switched-off setup never records again, it stayed off for 2 years. |
| 3 | **Trend length 50** | 10-day +0.06R, 20-day +0.10R, 50-day +0.11R, 100-day +0.09R, 200-day +0.10R | **The 50-day is the only length positive in every half-year**, with the lowest drawdown ($882 vs $1,172–1,555). The 10- and 20-day lengths were negative in 2024 H2. |
| 4 | **Uptime fix** (Task Scheduler autostart, or a server) | Roughly triples live signals | The bot missed about 10 of 15 October opens. No strategy change matters if the process isn't running at 9:30. |
| 5 | **Loss caps** (55-point max stop with tighten, no-follow-through cut) | On 43 days: worst trade −$168 → −$112, drawdown $373 → $223, profit about −25% | One trade with an 83-point stop cost 17% of a $1,000 account. Capping trades a little profit for much less tail risk. |
| 6 | **Flat by 12:00** | Drawdown $719 → $584 for about $90 less profit | The afternoon adds variance, not edge. Same as the NY-morning signal services. |
| 7 | **Entries to 11:30, room filter off, `orb` back on (trend only)** | About +60–90 trades over 2 years at similar R | The room-to-level filter cost about 60 trades with no gain under the trend rule. `orb` alone averaged +0.24R on the earlier run, but only +0.04–0.16R on 14–15 trades with full history, so it's a small sample. |
| 8 | **Liquidity final target** (nearest level 1.8–4R, else 2R) | Slightly better than a fixed 2R | Price tends to travel to obvious levels. Members also get a named target. |
| 9 | **A/B grade** | A (all three trends agree) +0.14R on 401 trades; B +0.08R on 151 | Useful for sizing or confidence. Kept as information, because filtering to A only loses trades. |
| 10 | **Stale-message guard and restart lock** | Prevents posting a 3-hour-old signal after a restart | On Oct 1 a restart re-posted the 9:50 signal at 13:04. A copier would have opened the trade then. |

### 3.2 Tested and not adopted

| Idea | Result | Verdict |
|---|---|---|
| Second with-trend entry after a win | +0.07–0.10R, but some half-years negative | Not robust |
| Sweep-and-reclaim setup (PDH/PDL/ONH/ONL/OR) | No edge even trend-filtered; on 42 days it lost −138 points over 22 trades | Dropped |
| VWAP pullback setup | No edge | Off |
| 5-minute opening range | Worse | No |
| 30-minute opening range | Worse | No |
| 10-minute opening range | More money, +55% drawdown | No (risk-adjusted worse) |
| Meta-switching between old and new rules by recent results | Worse than the trend rule alone | No |
| Trading only when old and new rules agree | +0.18R but half the trades | No (frequency matters) |
| Target 1 at 1R | +0.17R vs +0.15R (warm-up-excluded run), but 58% wins | Kept 0.7R for the owner's 65–70% win-rate goal. **A good choice if you optimize for profit.** |
| Early-session momentum rule | +0.03–0.08R over 2 years | No |
| Minimum follow/fade edge 0.5–1R (old rule) | Hurt September | No |
| Never trading against the day's lean (old rule) | Fewer trades, less profit | No |
| Skipping wide stops instead of tightening them | 18 trades, +$430 vs +$987 | Tighten wins |
| Tighter caps (40–50 points) without the room filter | 50 points +$984; 40 points +$572 | 55 points kept |

### 3.3 Earlier findings from the first 42 sessions (Aug 3 – Oct 1, 2026, real MNQ bars)
These motivated the first version. They didn't all survive the 2-year test.
- **The opening drive (9:30–9:45) carried on to 10:30 only 39% of the time.**
- **Following the first range break lost on average,** about −28 points an hour later. Always fading made +0.18R; always following lost −0.16R. This was regime-specific: over 2 years, neither works without the trend side.
- **Streaks were real** (some weeks breakouts ran, others reversed), which motivated the adaptive pick. Over 2 years, the pick didn't beat a coin flip.
- **The daily "bias" call was right on only 44% of 52 days,** and "same as yesterday" on 56%. A day-ahead direction call from daily bars is noise. The 50-day trend works as a side selector for the opening break, not as a day forecast.

---

## 4. Detailed breakdowns (live configuration, full trend history, 566 trades)

### 4.1 By half-year
| Period | Trades | Win | Avg R | $ / micro |
|---|---|---|---|---|
| 2024 H2 | 117 | 64% | +0.13 | +976 |
| 2025 H1 | 123 | 67% | +0.10 | +1,061 |
| 2025 H2 | 126 | 69% | +0.14 | +1,114 |
| 2026 H1 | 124 | 64% | +0.13 | +1,412 |
| 2026 H2 (to Oct 21) | 76 | 63% | +0.12 | +916 |

### 4.2 By month ($ per micro)
- Negative months: Jul 2024 (−16), Aug 2024 (−353, the volatility shock), Jan 2025 (−247), Feb 2025 (−35), Nov 2025 (−154), May 2026 (−180), Sep 2026 (−38).
- Best months: Jan 2026 (+758), Aug 2026 (+646), Dec 2024 (+549).
- Median month +$207. About 20 trades a month.
- Max run of consecutive non-winning trades: 4. The old rule's was 7.

### 4.3 By entry time (10-minute buckets)
| Entry | Trades | Win | Avg R |
|---|---|---|---|
| 9:50 | 308 | 66% | +0.12 |
| 10:00 | 119 | 70% | +0.17 |
| 10:10 | 51 | 63% | +0.15 |
| 10:20 | 22 | 55% | −0.10 |
| 10:30–11:30 | 66 | 60–80% | about +0.08 (small buckets) |

**The edge is concentrated in 9:50–10:20.** This matches the TradingMind journal: its 9:xx entries were best, and 11:xx was close to zero.

### 4.4 Other slices
| Slice | Trades | Win | Avg R | Note |
|---|---|---|---|---|
| Follow (break with trend) | 266 | 66% | +0.13 | |
| Fade (break against trend) | 286 | 65% | +0.11 | Both legs pay |
| Long | 403 | 65% | +0.12 | Mostly an up-trending period |
| Short | 163 | 66% | +0.12 | **Fewer shorts: a long down-trend is less tested** |
| Grade A | 401 | 67% | +0.14 | |
| Grade B | 151 | 63% | +0.08 | |
| Regime "trend" / "range" | 400 / 166 | 66% / 66% | +0.12 / +0.13 | Regime label adds nothing here |
| Risk 20–30 points | 75 | 75% | +0.22 | Tighter opening ranges are better |
| Risk 30–40 points | 118 | 63% | +0.11 | |
| Risk 40–55 points | 370 | 65% | +0.12 | |
| Monday | 108 | 63% | +0.03 | Weakest day. Probably noise; not acted on |
| Thursday | 115 | 67% | +0.19 | Best day |

### 4.5 How exits make the money
| Exit | Trades | Avg R | Share |
|---|---|---|---|
| Stopped | 185 | −1.01 | 33% |
| Break-even after Target 1 | 196 | +0.35 | 35% |
| Closed at flat time | 76 | +0.67 | 13% |
| Final target | 100 | +1.40 | 18% |
| No-follow-through cut | 9 | −0.37 | 2% |

**What this shows:**
- **About 18% of trades (the runners that reach the final target) pay for all the stops.** Don't cut runners early, and don't move Target 1 so far out that fewer trades reach break-even.
- The "win rate" is mostly Target 1 hits. A trade that banks half at 0.7R and then scratches counts as a +0.35R win.

### 4.6 Old rule, same data, for contrast
- 387 trades, 59% wins, +0.03R, profit factor 1.13, +$1,273, drawdown $1,849, 14 of 28 months positive.
- By leg: shorts −0.05R (199 trades), longs +0.11R. Fade +0.01R, follow +0.05R.
- **Its one good stretch is recent:** 2026 H2 +0.33R on 51 trades, versus the new rule's +0.12R. A choppy, streaky market suits the adaptive pick.
  - Meta-switching between the rules was tested and lost.
  - **Monitor this:** if the trend rule keeps lagging for months, revisit.

---

## 5. Validation protocol (what made results trustworthy)

1. **Long sample over short sample.** 42 sessions gave +0.36R; 577 gave +0.03R for the same rule. **Don't accept any intraday rule on fewer than about 300 trades spanning several market regimes.**
2. **Walk-forward by half-year.**
   - Choose the free parameter (trend length) on all *earlier* half-years only, then test the next half-year. Chain the results.
   - Full history, candidates 10/20/50/100: 451 trades, 65% wins, **+0.11R, +$4,047 out of sample.**
   - **Watch the candidate set.** Adding 200-day lets it be picked on thin 2024 data (89 trades), and the out-of-sample result drops to +0.07R. Parameters with less history behind them should need more trades to be chosen.
3. **Stability across the parameter.** The result should vary smoothly with trend length (+0.06 / +0.10 / +0.11 / +0.09R for 10/20/50/100). A lone spike means overfitting.
4. **Stability across time.** Report every half-year and every month, not just the total. The old rule's total was positive but only 2 of 5 half-years were.
5. **Random-walk integrity check.** Run the exact strategy on structureless random-walk bars. The edge must vanish; it gave −0.02R. If it doesn't vanish, the simulator is leaking future data.
6. **Proxy validation.** Before trusting proxy-data results, replay the overlap period on both the proxy and the real instrument and compare trade-by-trade. Here: 26 of 26 trades matched on side.
7. **Conservative fills.** Slippage of 1 tick on entry and stop, commissions in, stop-first on ambiguous bars.
8. **The backtester runs the exact live strategy code** (same class, same bar events). There's no separate "research version" of the rules.
9. **Bootstrap on months** (block-resampled 21-session months). Useful for the shape of risk: P(losing month), drawdown percentiles. It only reshuffles the same days, so it isn't evidence of an edge.
10. **Be honest about selection.** The 50-day length was picked after seeing results. The walk-forward and the smooth progression across lengths mitigate that but don't remove it.

---

## 6. Bugs and traps (check any port against these)

### 6.1 Adaptive filter death spiral (critical)
- **What happened:** the filter disabled a setup after a losing run. Disabled means no trades, which means no new results, so the setup stayed disabled forever. Over 2 years: 14 trades total.
- **Fix:** when the filter blocks a trade, simulate it silently on paper with the same management and record its R in the book. Any "switch off" logic needs a way to see what it would have earned.

### 6.2 Look-ahead through the daily-history index (critical, silent)
- **What happened:** the "keep only days before today" filter ran only when the daily frame had a `DatetimeIndex`. A frame indexed by Python `date` objects skipped the filter, so every day saw the *final* trend.
  - Result: wildly wrong backtests, from −0.05R to +0.15R depending on the frame.
- **Fix:** coerce any index to datetimes, then always filter strictly before the trading day, with a test using a `date`-object index.
- **Lesson:** a filter guarding against look-ahead must fail loudly, never skip silently.

### 6.3 Warm-up hides hard periods
- **What happened:** with no daily history before the intraday data, the first 50 sessions (Jul–Sep 2024, including the Aug 2024 shock) had no trend, so almost no trades. Headline results looked better than full-history results: +0.146R and $584 drawdown vs +0.113R and $882 for the same rules.
- **Fix:** always load indicator history from before the test window. Report how many sessions were warm-up.

### 6.4 Short-window warm-up mismatch
- **What happened:** a 50-day trend on a 43-day real-MNQ CSV produced only 7 trades.
- **Fix:** prepend proxy history, or pass a separate long daily series.

### 6.5 Stale settings resurrecting an old rule
- **What happened:** a saved `alpha_params.json` from the old regime could switch the old rule back on.
- **Fix:** version the settings schema. Ignore files missing the new key (`orx_policy`), and protect loss-control keys from being loosened.

### 6.6 Late and replayed signals
- **What happened:**
  - A restart recomputed the day's mode and replayed the morning, posting a 9:50 signal at 13:04.
  - A dropped stream delivered a 9:55 signal at 10:04.
- **Fix:** a stale-bar guard (120 seconds), locking the day's decisions on restart, and a real-time quote-polling fallback before any delayed-bar fallback.

### 6.7 Uptime
- **What happened:** the bot was down at the open on about 10 of 15 sessions (late manual starts, a laptop asleep).
- **Fix:** an OS-level autostart at logon and on weekdays at 9:00 with wake-to-run and restart-on-failure, or a VPS.
- **A strategy that trades in the first 30 minutes needs process supervision more than features.**

### 6.8 Data plumbing
- **Futures roll splicing:** a daily vendor (investing.com) kept quoting the expiring September contract through Sep 14–18 while volume had moved to December.
  - Detect it by low volume in expiry week. Drop those rows and back-adjust earlier prices by the carry offset (+243.75 points).
  - Cross-check against signal-service entry prices.
- **API rate limits:** Twelve Data allows 8 credits a minute on the plan used. Paginate by quarter, wait and retry, and drop partial days.
- **Timezones:** compare calendar dates in exchange time (America/New_York), and handle naive and tz-aware indexes explicitly.
- **SQLite live DB on a synced or locked folder:** a "disk I/O error" when reading it read-only. Copy the DB and its WAL file, then read the copy.

---

## 7. Reverse-engineering other signal services

### 7.1 TradingBrain (3 signals, Oct 16–20, 2026, from their Discord posts)
- **Structure:**
  1. Read a higher-timeframe bias first (e.g. "BEARISH (HIGH) · continuation").
  2. Wait for the open's counter-trend sweep or run.
  3. Enter *with* the bias.
  4. Target 1 at about 1R, a liquidity-level final target, flat by noon, one trade a day.
- **What we took (testable pieces):**
  - Bias from the higher-timeframe trend (became the 50-day trend side)
  - Trading both legs of the opening break relative to that bias
  - Liquidity final targets
  - The noon cut-off
- **What we couldn't test:** their exact sweep definition. Our sweep setup showed no edge even trend-filtered, so the bias, not the sweep, seems to be where the edge is.

### 7.2 TradingMind (DiscordKit journal export, 123 trades, Jul 2 – Oct 13, 2026, their own paper numbers)
- **Overall:** +3,484 points, 58% wins, +28 points per trade.
- **By time** (their strength is the first half hour):
  - 9:xx: 47 trades, 68% wins, +49 points per trade
  - 10:xx: 55 trades, +18
  - 11:xx: 21 trades, +9
- **By trend** (QQQ 50-day): most of their profit comes from trades against the trend.
  - Against: 58 trades, 67% wins, +46 points per trade
  - With: 65 trades, 49% wins, +12
- **Signature:** shorts in an up-trend made 42 trades, 76% wins, +55 points per trade (fading strength at the open). Shorts in a down-trend made only +3 per trade.
- **By sequence:** 1st trade of the day +40 points per trade (66 trades); 2nd +18 (36 trades); 3rd and later +8 (21 trades).
- **On the earlier 108-trade version:**
  - The rule "stop after the first loss and no entries 10:30–11:30" kept profit flat and cut drawdown from 303 to 89 points (fit on Jul–Aug).
  - On unseen September it gave +788 vs +687 points, with drawdown 57 vs 103.
  - Any trade after a loss that day: 23% wins, −2.5 points.
- **Correlation with our bot:** daily P&L correlation was 0.04. **It's a different, complementary edge.** Don't apply our trend filter to a TradingMind copy, because it would remove their best trades.
- **Mapping their journal outcome field:** `pt3_hit`, `target` and `closed` are runners; `stopped_be` and `break-even` are scratches.

### 7.3 General method for decoding a signal service
1. Export the full message history (DiscordKit JSON beats a text export: timestamps and edits survive).
2. Parse each trade into: time, direction, setup label, entry, stop, targets, outcome, points.
3. Slice by: time of day, trade number in the day, after-a-loss, direction vs daily trend, setup label, month.
4. Fit day-level rules (cut-offs, stop-after-loss) on the first two-thirds by date, and test on the rest.
5. Correlate its daily P&L with your own to see whether it's a different edge (combine) or the same one (pick one).

---

## 8. Live paper record (October 2026, too small to judge)
- 5 trades: −1.0, +0.35, −1.0, +0.34, +1.35 R.
- Running at the open on only about 5 of 15 sessions.
  - Late starts: 10/2, 10/7, 10/9, 10/20
  - Not running: 10/5, 10/6, 10/8, 10/12–14, 10/21
- **10/16:** the stream went quiet and the fallback used delayed bars. The 9:55 signal was processed at 10:04 and correctly held back as stale.
- **10/1:** a short at 30,709 with an 83-point stop, 29 points above support, was stopped in 6 minutes. This led to the 55-point risk cap and the room checks.
- **Recent regime:** Sep 2026 was −$38 and Oct +$8 for the new rule, while the old adaptive rule did better (Aug–Oct real MNQ +0.34R vs +0.06R). Keep paper trading. Re-check monthly.

---

## 9. Reproduction recipe

```bash
# in the source repo (Windows: activate .venv first)
abg alpha sanity                                            # random-walk integrity check
abg alpha backtest --csv data/mnq_proxy_5m.csv --walk-forward   # 2-year proxy data; QQQ daily trend auto-downloaded
abg alpha backtest --csv data/mnq_proxy_5m.csv --trend-daily qqq_daily.csv --trades
abg alpha replay --csv data/mnq_5m.csv --date 2026-09-23   # exact messages for one day
abg alpha journal bot-journal_TradingMind.json              # day-rule analysis on a service's journal
python -m pytest -q tests/                                  # 219 tests
```

- **Proxy dataset:** QQQ 5-minute bars (Twelve Data, Jul 2024 – Oct 2026) × 41.2456, rounded to 0.25. 44,794 bars, 577 sessions. Git-ignored at `data/mnq_proxy_5m.csv`.
- **Trend history:** QQQ daily closes from Dec 2023, × the same ratio. The sign is unaffected by the ratio.
- **Code map:**

| File | What it holds |
|---|---|
| `abg/alpha/strategy.py` | Rules, parameters, management, paper tracking |
| `abg/alpha/context.py` | Daily context and trends |
| `abg/alpha/learn.py` | Adaptive book |
| `abg/alpha/regime.py` | Old follow/fade replay |
| `abg/alpha/backtest.py` | Simulator, walk-forward, adoption rule |
| `abg/alpha/bot.py` | Live loop, feeds, Sunday re-fit |
| `abg/alpha/messages.py` | Discord text |
| `docs/14-alpha-mnq-bot.md` | Full narrative |

---

## 10. Porting checklist for a new project
- [ ] Use the same bar construction: 5-minute bars, a 15-minute opening range from 9:30, exchange-time dates.
- [ ] Use a trend input with at least 50 prior closes, filtered strictly before the trading day. Unit-test the filter with a `date` index and a tz-aware index.
- [ ] Choose the side from the trend, not from recent follow/fade results.
- [ ] Use stop = 0.8 × opening ATR, bounded 12–55 points, and tighten rather than skip.
- [ ] Use Target 1 at 0.7R (or 1R if optimizing profit) with half off and break-even after, a liquidity final at 1.8–4R else 2R, a 1.5 × ATR5 trail after +1.5R, and the 20-minute no-follow-through cut.
- [ ] Apply the day rules: entries 9:35–11:30, flat 12:00, at most 2 trades, stop after the first loss, 1.5R daily cap, news blackout, FOMC half size.
- [ ] Any self-disabling filter must keep scoring the trades it blocks (paper).
- [ ] Any automatic re-fit must be walk-forward, require minimum trades and out-of-sample profit, beat the current settings, and never loosen loss controls.
- [ ] Add a stale-signal guard, a restart lock, a real-time fallback feed, and process supervision (autostart or VPS).
- [ ] Before trusting the port, run the random-walk check, reproduce the half-year table within noise, and paper trade.

## 11. Open questions and next experiments
1. **Longer history:** real 1-minute MNQ for 3–5 years, to replace the proxy and include a sustained bear market (2022). Shorts are under-tested.
2. **Sunday re-fit on the long history** instead of 60 days, with the new days appended each week.
3. **Size by grade** (A full, B half) instead of equal size. Grade A averaged +0.14R vs B +0.08R.
4. **Combine with TradingMind** as a portfolio: the edges are uncorrelated. Test combined drawdown before any copying.
5. **Track old vs new rule live** and alert if the trend rule trails for 3 or more months (the regime watch).
6. **Tight opening ranges** (risk 20–30 points) were best (+0.22R). Test whether to size up when the opening range is tight.
7. **Monday weakness** (+0.03R) and **10:20 entries** (−0.10R): probably noise. Re-check with more data before acting.

*Educational research log; not financial advice. All figures are simulated or paper.*

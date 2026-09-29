# 12. Markets, real-time tracking, portfolio risk and learning

This chapter covers the cross-asset layer (futures, rates & bonds, FX, crypto), real-time prices,
the economic calendar, portfolio-level and prop-firm risk, entry confirmation, AI help, chart
images, Discord slash commands, scheduled posts, backtesting, model training and the broker bridge.

## 12.1 Instruments: futures, bonds, FX, crypto (`markets/instruments.py`)

Every symbol resolves to an `InstrumentSpec`: asset class, dollars per point (multiplier), tick size
and value, trading session and micro contract.

| Class | Examples | $ per 1.0 move | Notes |
|---|---|---|---|
| Equity index futures | ES, **NQ**, YM, RTY; micros MES, **MNQ**, MYM, M2K | ES $50, NQ $20, MNQ $2 | CME Globex hours |
| Treasury futures | ZT, ZF, **ZN**, TN, ZB, UB | $1,000 (ZT $2,000) per point | ticks 1/32 to 1/256 |
| Energy / metals / ags | CL, MCL, NG, GC, MGC, SI, HG, ZC, ZS, ZW | CL $1,000, GC $100, NG $10,000 | |
| Currency futures | 6E, M6E, 6J, 6B, 6A, 6C | 6E $125,000 | |
| Yields / indexes | ^IRX, ^FVX, ^TNX, ^TYX, ^VIX, ^GSPC, DX-Y.NYB | n/a | context, not traded |
| Bond ETFs | SHY, IEF, TLT, TIP, LQD, HYG, AGG, BIL … | $1 per share | |
| Crypto / FX spot | BTC-USD, ETH-USD, EURUSD=X | 1 | fractional units |

Accepted spellings: `NQ`, `/NQ`, `NQ1!`, `NQZ26`, `NQZ2026`, `MNQ`. Some roots are also stock
tickers (CL = Colgate, ZS = Zscaler, PL, GC, SI …). Those map to futures only with a futures hint:
`/CL`, `CL1!`, a month code, or the words *futures / contracts / ticks / handles / micros* in the post.

**Sizing.** Positions are sized in whole contracts:
`units = floor(risk budget / (|entry − stop| × $ per point))`.
If one full contract is over the budget, the tracker switches to the micro. For example, a 70-point
NQ stop is $1,400 on NQ but $140 on MNQ. It still tracks the NQ price, but sizes and reports in MNQ.
Paper P&L is in dollars (points × multiplier × contracts), and messages show the stop in points,
ticks and $ per contract. `ABG_PROP_MAX_CONTRACTS` caps contracts per position.

## 12.2 Markets overview and market regime (`markets/overview.py`)

The **Markets** tab (and `abg markets`) shows:
- equity index futures, Treasury futures, the yield curve with its 10y−3m and 30y−5y spreads
  (inversion flagged), bond ETFs, energy, metals, FX with the dollar index, crypto and the VIX
- each future's $/point and tick value

The **market regime** is S&P 500 and Nasdaq-100 trend (above/below their 50- and 200-day averages),
VIX level and 1-day change, and the 10-year yield's 1-month move. It is labelled risk-on,
constructive, mixed or risk-off, and cached for 15 minutes. With `ABG_EXT_MARKET_FILTER=true` it
feeds the entry grade: −1 for a long in a risk-off tape, −0.25 when mixed, +0.25 when risk-on
(mirrored for shorts). It also appears as a "Market:" line in every message.

## 12.3 Real-time prices (`live/stream.py`)

With `ABG_FINNHUB_API_KEY` set, the monitor opens Finnhub's websocket and streams trades for every
tracked stock, ETF, crypto and FX symbol. Trades are batched each second, and each batch carries
the high and low seen since the last one. A spike through a stop or target between two batches still
counts: a wick is a wick.

- **Futures, yields and indexes** aren't on the free stream. They are polled every
  `ABG_EXT_FAST_POLL_SECONDS` (15 s) while their session is open. So is everything else whenever the
  stream is down.
- **Reliability:** the stream reconnects with backoff (1 s → 60 s), re-subscribes when the tracked set
  changes, and reconnects if it goes 90 s without data during market hours.
- **Fallback:** the regular 60-second sweep keeps running underneath as a backstop.
- **Status:** shown in `/api/monitor` under `external.stream`.

## 12.4 Economic and earnings calendar (`markets/calendar.py`)

- **FOMC decisions:** the Fed's published 2026–2027 schedule, 14:00 ET on the second day.
- **CPI and jobs reports:** the BLS 2026 schedules, 08:30 ET. Later months are **estimated** (jobs:
  first Friday, moved past holidays; CPI: around the 12th) and labelled "(estimated)".
- **Your own events:** `ABG_DATA_DIR/calendar.json`, for example
  `[{"date": "2027-01-13", "time": "08:30", "name": "CPI (inflation)"}]`. These replace estimates for
  the same name and month.
- **Earnings:** looked up automatically for tracked symbols through Finnhub
  (`ABG_EXT_AUTO_EARNINGS=true`, cached for 12 h). A date given in the post always wins.
- **Effects on tracking:**
  - No new entries from `ABG_EXT_EVENT_BLACKOUT_BEFORE_MIN` (30) minutes before a high-impact release
    to `ABG_EXT_EVENT_BLACKOUT_AFTER_MIN` (15) minutes after it.
  - Earnings warnings and an entry pause around each report (chapter 11).
- **Posts and views:**
  - Sunday "week ahead" post and a weekday morning "today's releases" post.
  - The Markets tab calendar, and `abg calendar -s NVDA -s AAPL`.

## 12.5 Portfolio-level risk and prop-firm rules (`extsignals/riskgate.py`)

Before any entry fills, the whole book is checked, not just the one idea:

| Check | Setting (default) | When it fails |
|---|---|---|
| open positions | `ABG_EXT_MAX_OPEN_POSITIONS` (8) | blocked |
| heat: $ at risk to all stops, including this trade | `ABG_EXT_MAX_HEAT_PCT` (6% of the account) | **shrunk to fit** (`ABG_EXT_RESIZE_TO_FIT`), else blocked |
| correlated bets: 90-day daily-return correlation ≥ 0.7, same direction | `ABG_EXT_MAX_CORRELATED` (2) | blocked, names the overlapping trades |
| sector / futures group | `ABG_EXT_MAX_SECTOR` (3) | blocked |
| prop firm: daily loss limit | `ABG_PROP_DAILY_LOSS_LIMIT` ($) | shrunk to fit 90% of what's left, else blocked |
| prop firm: trailing drawdown | `ABG_PROP_MAX_DRAWDOWN` ($), `ABG_PROP_DRAWDOWN_MODE` eod/intraday, `ABG_PROP_DRAWDOWN_LOCK` | same |

**Equity** is paper equity: account + realized P&L + open P&L. The day's starting equity and the
high-water mark are stored, so they survive restarts. With `ABG_PROP_DRAWDOWN_LOCK=true` the
trailing floor stops rising once it reaches the starting balance, like most evaluation accounts.

**Warnings.** At `ABG_PROP_WARN_PCT` (70%) of the daily limit, the drawdown or the heat, a warning is
posted to Discord and the dashboard, once per day per level. Reaching a limit posts a critical alert
and blocks new entries.

**Where to see it:** the Signals tab's *Risk book* panel, `abg ext risk`, and `/abg risk` in Discord.

Set your firm's exact numbers. They differ by firm and account size:

```
ABG_PROP_ENABLED=true
ABG_PROP_ACCOUNT_SIZE=50000
ABG_PROP_DAILY_LOSS_LIMIT=1000
ABG_PROP_MAX_DRAWDOWN=2500
ABG_PROP_DRAWDOWN_MODE=eod
ABG_PROP_MAX_CONTRACTS=8
```

## 12.6 Entry confirmation on a shorter timeframe (`extsignals/confirm.py`)

With `ABG_EXT_CONFIRM_TIMEFRAME=1h` (the default), a zone entry doesn't fill on the first touch. It
waits for a completed 1-hour bar near the zone that shows a reversal:
- a bullish bar closing above the prior bar's high, or
- a close back above the 9-EMA after being below it.

Shorts mirror this. While it waits, one ⏳ *waiting for confirmation* message is posted.

- **No stall:** after `ABG_EXT_CONFIRM_MAX_WAIT_HOURS` (24) in the zone it enters anyway.
- **No data:** if intraday data isn't available it enters, and says so.
- **Scope:** breakouts and market entries aren't delayed. Set `15m` for faster confirmation, or
  `none` to fill on touch.

## 12.7 AI reading and narration (`extsignals/ai.py`)

This part is optional and needs `ANTHROPIC_API_KEY`.

- **Parse fallback.** When the rule parser finds no plan in a post, Claude extracts the signals as
  JSON. Every level it returns must appear literally in the post, so any invented number drops that
  signal. Ideas read this way carry a "double-check the levels" warning.
- **Narration.** Key messages (ingest, entry, block, suggestion, exits, advisories) get a 2–3 sentence
  plain-English lead. It is written only from the structured analysis already in the message, never
  from the model's own market knowledge.

Both have short timeouts and fail silently. Turn them off with `ABG_EXT_AI_PARSE=false` and
`ABG_EXT_AI_NARRATIVE=false`.

## 12.8 Chart images (`extsignals/charts.py`)

Discord messages for ingest, entries, adds, exits, entry changes and advisories carry a chart. It
shows 90 daily candles with the zone band, the stop (and warning level), targets (the scale-out
level dotted), fill markers and the last price. It needs `matplotlib`, which is included in `[all]`.
Turn it off with `ABG_EXT_RELAY_CHARTS=false`.

## 12.9 Discord slash commands (`extsignals/gateway.py`)

The bot registers `/abg` on the signal channel's server and answers over its gateway connection, so
no public URL is needed.

| Command | Who can use it |
|---|---|
| `/abg ideas`, `/abg status symbol:`, `/abg stats`, `/abg risk`, `/abg markets` | anyone in the server |
| `/abg track text:`, `/abg close id: [fraction:]`, `/abg cancel id:`, `/abg stop id: price:` | only user ids in `ABG_DISCORD_ADMIN_IDS`; others get a private "not allowed" |

The invite URL needs the **`applications.commands`** scope in addition to `bot`. To get your user id:
Developer Mode on → right-click your name → Copy User ID.

## 12.10 Scheduled posts, backups, health (`live/scheduler.py`)

| When (ET) | What |
|---|---|
| Friday 16:15 | weekly recap: new / closed ideas, win rate, R, best / worst, by source and by signal type, open risk |
| Sunday 18:00 | week ahead: macro releases + earnings for tracked symbols |
| weekdays 07:30 | today's high-impact releases, only when there are any |
| daily at `ABG_BACKUP_HOUR` (2) | online SQLite backups to `ABG_DATA_DIR/backups/YYYY-MM-DD/`, keeping `ABG_BACKUP_KEEP` (14) days |
| every 5 min | ping `ABG_HEALTHCHECK_URL` (dead-man's switch); `/fail` when degraded |
| every minute | watchdog: at most one alert per hour when quotes stop updating or the stream has been down 10+ min in market hours |

## 12.11 Backtesting your sources (`extsignals/backtest.py`)

```bash
abg ext backtest signals.txt                 # .txt / .json / .csv of dated messages
abg ext backtest --discord 123456789 --limit 500    # a channel's history (bot token needed)
abg ext backtest signals.txt --gate C        # replay with the entry-grade gate on
```

**Text file format.** Put a header line before each message: `### 2026-08-12 14:30 author`, then the
message text. JSON and CSV files use `ts|date|timestamp`, `text|content` and `author` fields.

**How the replay works:** day by day, with the same parser, validation, classification, scale-in /
scale-out, lifecycle and grading as live tracking, and no look-ahead:
- Each idea is prepared with data known when it was posted: the prior close, or that day's close if
  it was posted after 16:00 ET.
- It starts trading on the next bar.
- Grading uses history cut at that date: indicators, composite signal, regime, simulated odds.
- Source updates ("stop to BE", "closing here") apply at that day's close.

**Report contents:**
- overall fill rate, win rate, average and total R, max drawdown in R, holding time
- tables by source, pattern, basis and grade, with a note on whether D-grades really underperform
- an equity curve in R

Reports are saved to `ABG_DATA_DIR/backtests/` (JSON + CSV) and listed on the Signals tab.

## 12.12 Training the models (`abg train …`)

| Model | Command | Learns | Used when |
|---|---|---|---|
| **Entry grade** (`models/grade_model.json`) | `abg train grade` | P(win) and expected R from the setup at decision time: trend, signal, R:R, RSI, stop and entry distance in ATR, regime, resistance before the target, pattern, basis, simulated odds, model view | out-of-sample AUC > 0.55 on ≥ 200 trades (`ABG_EXT_GRADE_MODEL=auto`); otherwise the rule grade stays in charge |
| **Risk** (`models/risk_model.json`) | `abg train risk` | P(≥ 10% drawdown within 21 trading days) and 21-day volatility, from the 40-feature risk schema | always once trained; shown next to the baseline in every analysis |

- **Training data:**
  - **Grade model:** the terminal's own setups simulated over years of daily bars for a 50-symbol
    basket (every 5th bar), plus every trade from your saved backtests.
  - **Risk model:** the basket's full daily history.
- **Method:** plain-numpy logistic and ridge regression on standardised features with L2
  regularisation.
- **Validation:** walk-forward (earliest 70% train, latest 30% test, with a gap so labels don't
  overlap). The out-of-sample metrics are saved with the model and shown in the Markets tab and CLI.
- **When grades change:** the tracker reloads the grade model when the file changes. The learned
  grade shows up in the reasons ("learned model: 58% win odds, +0.21R expected (rule grade C)").

`abg train all` does both. Re-train monthly, or after a batch of new backtests.

## 12.13 Broker bridge: Alpaca paper trading (`brokers/alpaca.py`)

With `ABG_BROKER=alpaca_paper` and paper API keys, tracked decisions are mirrored as real paper
orders:
- entries and adds as market orders, plus a GTC protective stop
- stop moves replace that stop
- target slices and trims are sold at market
- exits cancel the stop and close the position

It uses the paper endpoint unless you explicitly set `ABG_ALPACA_LIVE=true` (don't until paper
results hold up). Stocks, ETFs and crypto only; futures ideas are skipped with a note. Each
event's broker result is logged on the event (`relayed.alpaca`).

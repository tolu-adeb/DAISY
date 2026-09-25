# 9. Saved portfolio & live signals

The terminal keeps a **saved portfolio** (holdings, watchlist, stops/targets, alert rules, signal
history) in a real database, and a **live monitor** watches it continuously. The monitor sends
signals to the dashboard, desktop pop-ups, Discord and email. The same monitor also tracks **external
trade ideas** parsed from messages or Discord channels (see [docs/11](11-external-signals.md)).

```
         ┌──────────── portfolio.sqlite3 (ABG_DATA_DIR) ────────────┐
 CLI ───►│ transactions · position_meta · watchlist · alert_rules   │◄─── dashboard / REST
         │ signals (history) · signal_state (edge-trigger memory)   │
         └───────────────────────────┬──────────────────────────────┘
                                     │ symbols to track
             ┌───────────────────────▼────────────────────────┐
             │ Monitor (abg serve  or  abg monitor)            │
             │  quote sweep  every 60 s  (market hours)        │──► SignalEngine ──► NotificationHub
             │  analysis sweep every 15 min (live bar spliced) │                     ├─ dashboard (SSE + pop-ups)
             └─────────────────────────────────────────────────┘                     ├─ desktop (plyer)
                                                                                     ├─ Discord webhook
                                                                                     └─ email (SMTP digest)
```

## 9.1 Saved portfolio

Data lives in `ABG_DATA_DIR/portfolio.sqlite3` (default `~/.abg-terminal`; set `ABG_DATA_DIR=data` to keep
it inside the project folder). It is **not** the cache: `abg cache clear` never touches it.

- **Transactions are the source of truth.** Positions are derived from them with the average-cost method.
  Fees are added to cost on buys and subtracted from proceeds on sells, and realized P&L is tracked.
  Overselling is rejected, and so is deleting a buy that a later sell depends on.
- **Watchlist**: tickers with no position that the monitor still tracks.
- **Stop-loss / take-profit** per holding, which trigger critical / warning alerts.
- **Multiple portfolios**: use `-P name` on the CLI or `?portfolio=name` on the API (default `main`).
- **Backup**: `abg portfolio export -o backup.json` / `abg portfolio import backup.json`. The SQLite
  file itself is also safe to copy while nothing is running.

```bash
abg portfolio buy AAPL 10                 # price defaults to the live quote
abg portfolio buy MSFT 5 --price 410 --date 2026-06-02 --fees 1
abg portfolio sell AAPL 4
abg portfolio watch NVDA AMD TSLA
abg portfolio set MSFT --stop 380 --target 470
abg portfolio show                        # live P&L, weights, portfolio VaR   (--analyze adds signal/risk per row)
abg portfolio history                     # transaction log  →  abg portfolio remove-tx 7
abg alert add NVDA price_above 150        # one-shot by default; --repeat to keep it
abg alert add TSLA rsi_below 30 --repeat --note "oversold watch"
abg alert list  |  abg alert kinds  |  abg alert remove 3
abg signals                               # saved signal history
```

In the dashboard, the **Portfolio** tab has the same functions. You can record trades (leave the price
blank to use the live quote), edit stop/target inline, add or remove watchlist tickers and rules, and
see the live signal feed.

## 9.2 Running the monitor

| How | Behaviour |
|---|---|
| `abg serve` (or **Start Dashboard.bat**) | Dashboard + API + monitor in one process. Signals appear live in the dashboard. |
| `abg serve --no-monitor` | Dashboard only. It still shows saved signals from a monitor running elsewhere. |
| `abg monitor` (or **Start Monitor.bat**) | Headless monitor in a console window, printing each signal. Good for leaving running. |
| `abg monitor --once` | One full sweep, then exit (e.g. from Windows Task Scheduler every 15 min). |

**Only one monitor runs per data folder.** A heartbeat lock file (`monitor.lock`) stops `abg serve` and
`abg monitor` from both sending the same alert. The second one reports "running elsewhere" and just
displays signals.

**Keeping it running on Windows:** leave the window open. The computer must be awake, and the monitor pauses
while it sleeps. To start it at logon, open *Task Scheduler → Create Basic Task → When I log on → Start a
program* and pick `Start Monitor.bat`.

### Sweeps and API budget

| Sweep | Default interval | What it does | Calls per symbol |
|---|---|---|---|
| Quote | 60 s during NYSE hours, 30 min outside | Live quotes → price-based signals and a portfolio snapshot | 1 quote |
| Analysis | 15 min (market hours) | Full analysis with the **live quote spliced into today's daily bar**, so RSI, MACD, setups and risk are current intraday | news + (cached) history/fundamentals |

Daily history is reused for `ABG_MONITOR_HISTORY_TTL` (6 h), so the monitor mostly spends quote calls.
With Finnhub first for quotes (60/min free), about 50 symbols at a 60-second interval fit the free
budget. Beyond that the router fails over to Twelve Data or Yahoo automatically. The calendar knows
NYSE holidays (Good Friday, observed July 4th and so on), and the monitor refreshes everything at the open and close.

## 9.3 Signal catalogue

Signals are **edge-triggered**: they fire when something *changes*, not on every sweep while it stays
true. The last value of each condition is saved, so restarting doesn't re-fire. Transition signals stay
silent on the first observation of a symbol, because there's nothing to compare against. A per-signal
cooldown (`ABG_SIGNAL_COOLDOWN`, 4 h) stops a condition that keeps flipping back and forth from spamming.

| Kind | Fires when | Severity |
|---|---|---|
| `signal_change` | Composite label changes (e.g. Neutral → Bullish) | warning if "Strong", else info |
| `setup` | A trade setup newly qualifies at ≥ 75% confidence (includes entry/stop/targets) | warning (directional) |
| `rsi` | RSI(14) enters overbought (≥ 70) or oversold (≤ 30) | info |
| `macd_cross` | MACD histogram changes sign | info |
| `ma_cross` | Golden / death cross (SMA50 vs SMA200) | warning |
| `trend_break` | Price crosses its 200-day SMA | info |
| `risk_change` | Baseline risk level changes (Elevated+ = warning) | info / warning |
| `sentiment` | News sentiment turns clearly positive or negative (≥ 3 articles) | info |
| `big_move` | Intraday move crosses each ±`ABG_PRICE_MOVE_PCT` band (3%, 6%, 9%…) | warning |
| `level_break` | Price breaks above the nearest resistance or below the nearest support | info / warning |
| `stop_hit` | Holding price ≤ your stop-loss | **critical** |
| `target_hit` | Holding price ≥ your take-profit | warning |
| `position_loss` | Holding falls another `ABG_POSITION_LOSS_PCT` (8%) below cost | warning |
| `portfolio_move` | Portfolio day change crosses each ±`ABG_PORTFOLIO_MOVE_PCT` (2%) band | warning |
| `concentration` | A holding exceeds `ABG_CONCENTRATION_PCT` (35%) of a ≥ 3-position portfolio | info |
| `recommendation` | The prediction model's view changes (e.g. Hold → Buy), with the thesis headline | info / warning |
| `custom_rule` | Your rule: `price_above/below`, `change_above/below`, `rsi_above/below`, `score_above/below` | warning |

Signals are rule-based, educational analytics, **not trade instructions**.

## 9.4 Notification channels

| Channel | Setup (in `.env`) | Default minimum severity |
|---|---|---|
| Dashboard feed | none. Click **Enable pop-ups** once to get browser desktop notifications while the dashboard is open | info |
| Desktop (native) | `pip install plyer` (included in `.[all]`); `ABG_NOTIFY_DESKTOP=true` | warning (`ABG_NOTIFY_DESKTOP_MIN_SEVERITY`) |
| Discord | `ABG_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...` | info (`ABG_NOTIFY_DISCORD_MIN_SEVERITY`) |
| Email | `ABG_SMTP_HOST`, `ABG_SMTP_PORT`, `ABG_SMTP_USER`, `ABG_SMTP_PASSWORD`, `ABG_EMAIL_TO` (+ optional `ABG_EMAIL_FROM`, `ABG_SMTP_SSL`) | warning (`ABG_NOTIFY_EMAIL_MIN_SEVERITY`) |

- **Discord webhook**: in your server, go to *Server Settings → Integrations → Webhooks → New Webhook*, pick the channel,
  and click *Copy Webhook URL*. Each signal is posted as an embed coloured by direction, with price, level and score fields.
  Posts are rate-limited to about 25 per minute and a 429 is retried.
- **Gmail**: turn on 2-Step Verification, create an **App Password** (Google Account → Security → App passwords), then set
  `ABG_SMTP_HOST=smtp.gmail.com`, `ABG_SMTP_PORT=587`, `ABG_SMTP_USER=you@gmail.com`,
  `ABG_SMTP_PASSWORD=<16-char app password>` and `ABG_EMAIL_TO=you@gmail.com`. Outlook uses `smtp.office365.com:587`.
  Non-critical signals are bundled into one digest every `ABG_EMAIL_BATCH_SECONDS` (120 s). **Critical ones (stop hit)
  go out immediately.**
- Test all channels: `abg notify-test` or the **Test alert** button. Each signal records its delivery
  result per channel (`abg signals` shows "Sent to").

## 9.5 API

| Method & path | Purpose |
|---|---|
| `GET /api/portfolio?risk=true` | Snapshot: totals, holdings (live P&L, weights, signal/risk), watchlist, rules, portfolio risk, monitor status |
| `POST /api/portfolio/transactions` | `{symbol, side, shares, price?, fees?, date?, notes?}`, where a missing price means the live quote |
| `GET /api/portfolio/transactions` · `DELETE /api/portfolio/transactions/{id}` | Log / delete |
| `POST /api/portfolio/watchlist` `{symbol}` · `DELETE /api/portfolio/watchlist/{symbol}` | Watchlist |
| `PUT /api/portfolio/positions/{symbol}` | `{stop_loss?, take_profit?, notes?, clear?}` |
| `GET /api/portfolio/export` · `POST /api/portfolio/import` | Backup / restore |
| `GET/POST /api/alerts` · `DELETE /api/alerts/{id}` | Custom rules (`kinds` listed in the GET response) |
| `GET /api/signals?limit&symbol&since` · `POST /api/signals/ack` | Signal history / mark read |
| `GET /api/signals/stream` | **Server-Sent Events**: `signal`, `snapshot`, `status` events (15 s keep-alive) |
| `GET /api/monitor` · `POST /api/monitor/start` · `/stop` · `/refresh` | Monitor control |
| `POST /api/notify/test?channel=all` | Send a test alert |

Portfolio edits made through the API wake the monitor immediately, so a new symbol is analysed within seconds.

## 9.6 Internals

- `abg/portfolio/store.py`: `PortfolioStore` (SQLite, WAL, thread-safe) and `compute_positions` (average cost).
- `abg/portfolio/valuation.py`: `snapshot()`, which gives live valuation plus `portfolio_risk` on current market-value weights.
- `abg/live/market_hours.py`: rule-based NYSE calendar (Easter-based Good Friday, observed-holiday rules).
- `abg/live/signals.py`: `SignalEngine.from_report / from_quote / from_snapshot`, band logic, cooldowns.
- `abg/live/notify.py`: `NotificationHub` (persist → SSE broadcast → per-channel background delivery with timeout and
  isolation) and the notifiers.
- `abg/live/monitor.py`: `Monitor` (two timers, market-hours pacing, poke/wake, error ring buffer, `MonitorLock`).
- `abg/engine.py::splice_quote`: merges a live quote into daily bars. It updates today's bar or appends one, and rejects
  quotes that jump more than 40% from the last close.

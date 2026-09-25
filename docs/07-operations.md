# 7. Operations

## 7.1 Configuration reference (`abg/config.py`)

Precedence: `Settings(...)` kwargs / CLI flags → environment (`ABG_*`) → `.env` in the working directory → defaults.

| Env var | Default | Meaning |
|---|---|---|
| `ABG_POLYGON_API_KEY`, `ABG_TIINGO_API_KEY`, `ABG_FMP_API_KEY`, `ABG_TWELVEDATA_API_KEY`, `ABG_ALPHAVANTAGE_API_KEY`, `ABG_FINNHUB_API_KEY` | – | Enable keyed vendors |
| `ANTHROPIC_API_KEY` (or `ABG_ANTHROPIC_API_KEY`) | – | Enables Claude insight |
| `ABG_ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Any Messages-API model id; set it to the current model your team uses |
| `ABG_AI_ENABLED` | `true` | `false` = always rule-based |
| `ABG_AI_TIMEOUT` / `ABG_AI_MAX_TOKENS` | 30 / 1200 | |
| `ABG_PROVIDER_ORDER` | `yahoo,polygon,tiingo,fmp,twelvedata,alphavantage,finnhub,yfinance,stooq,csv,synthetic` | Priority **and** allow-list |
| `ABG_QUOTE_ORDER`, `ABG_HISTORY_ORDER`, `ABG_NEWS_ORDER`, `ABG_OPTIONS_ORDER`, `ABG_FUNDAMENTALS_ORDER` | – | Per-capability priority + allow-list (overrides the global order for that capability) |
| `ABG_TIINGO_NEWS_ENABLED` | `false` | Tiingo News API is a paid add-on; free keys get 403 |
| `ABG_DISABLED_PROVIDERS` | – | Comma list to switch off |
| `ABG_POLYGON_BASE_URL` | `https://api.polygon.io` | e.g. the Massive domain |
| `ABG_CSV_DIR` | – | Folder for the `csv` provider |
| `ABG_ALLOW_SYNTHETIC` | `false` | Demo mode |
| `ABG_HTTP_TIMEOUT` / `ABG_CONNECT_TIMEOUT` | 8 / 4 s | |
| `ABG_MAX_RETRIES` | 1 | Retries inside one provider before failover |
| `ABG_RETRY_BASE_DELAY` | 0.25 s | Full-jitter backoff base |
| `ABG_HEDGE_DELAY` | 1.5 s | 0 disables hedging |
| `ABG_MAX_CONNECTIONS` | 32 | HTTP pool size |
| `ABG_BREAKER_FAILURES` / `ABG_BREAKER_COOLDOWN` | 3 / 60 s | |
| `ABG_CACHE_ENABLED` / `ABG_CACHE_DIR` | true / `~/.cache/abg-terminal` | |
| `ABG_MEMORY_CACHE_ENTRIES` | 512 | |
| `ABG_TTL_QUOTE`, `_HISTORY_INTRADAY`, `_HISTORY_DAILY`, `_NEWS`, `_OPTIONS`, `_FUNDAMENTALS`, `_AI` | 15, 60, 900, 600, 120, 86400, 3600 s | |
| `ABG_MAX_STALE` | 604800 s (7 d) | Stale-if-error horizon |
| `ABG_RISK_FREE_RATE` | 0.04 | Used by Sharpe and Black-Scholes. Update it when rates move |
| `ABG_DIVIDEND_YIELD_DEFAULT` | 0.0 | When fundamentals lack a yield |
| `ABG_BENCHMARK` | `SPY` | Beta, correlation, relative return |
| `ABG_WARMUP_DAYS` | 420 | Extra history for indicator warm-up |
| `ABG_NEWS_LIMIT` | 20 | |
| `ABG_RISK_MODELS` | – | `module:Attr,…` extra risk models |
| `ABG_FORECAST_PATHS` / `ABG_FORECAST_HORIZON` / `ABG_FORECAST_EQUITY_PREMIUM` / `ABG_FORECAST_SIGNAL_TILT` | 5000 / 63 / 0.05 / 0.25 | Prediction engine (docs/10) |
| `ABG_DATA_DIR` | `~/.abg-terminal` | Saved portfolio database + monitor lock (not the cache) |
| `ABG_DEFAULT_PORTFOLIO` | `main` | |
| `ABG_MONITOR_ON_SERVE` | `true` | `abg serve` also runs the live monitor |
| `ABG_MONITOR_QUOTE_INTERVAL` / `_ANALYSIS_INTERVAL` / `_OFFHOURS_INTERVAL` | 60 / 900 / 1800 s | Sweep pacing |
| `ABG_MONITOR_MARKET_HOURS_ONLY` | `true` | Slow down outside NYSE hours |
| `ABG_MONITOR_HISTORY_TTL` | 21600 s | Daily history reuse inside the monitor |
| `ABG_SIGNAL_COOLDOWN` | 14400 s | Minimum gap before the same signal re-fires |
| `ABG_PRICE_MOVE_PCT`, `ABG_POSITION_LOSS_PCT`, `ABG_PORTFOLIO_MOVE_PCT`, `ABG_CONCENTRATION_PCT`, `ABG_SETUP_MIN_CONFIDENCE` | 3, 8, 2, 35, 0.75 | Signal thresholds |
| `ABG_NOTIFY_DESKTOP` / `_MIN_SEVERITY` | true / warning | Native pop-ups (needs `plyer`) |
| `ABG_DISCORD_WEBHOOK_URL` / `ABG_NOTIFY_DISCORD_MIN_SEVERITY` | – / info | Discord alerts |
| `ABG_SMTP_HOST`, `ABG_SMTP_PORT`, `ABG_SMTP_USER`, `ABG_SMTP_PASSWORD`, `ABG_SMTP_SSL`, `ABG_EMAIL_FROM`, `ABG_EMAIL_TO`, `ABG_NOTIFY_EMAIL_MIN_SEVERITY`, `ABG_EMAIL_BATCH_SECONDS` | –, 587, –, –, false, –, –, warning, 120 | Email alerts |
| `ABG_EXT_*`, `ABG_DISCORD_BOT_TOKEN` | see [docs/11 §11.11](11-external-signals.md#1111-settings) | External signals: Discord channels, relay mode, paper sizing, time limits, entry-grade gate |
| `ABG_LOG_LEVEL` | `WARNING` | |

## 7.2 Running

```bash
abg serve --host 0.0.0.0 --port 8000                    # LAN access for the team
uvicorn abg.api.server:app --host 0.0.0.0 --port 8000 --workers 2
```

Notes for shared deployments:

- **Workers:** each worker has its own engine, memory cache and breakers. The SQLite disk cache is
  shared safely (WAL mode). One worker is plenty for a club and gives the best cache and coalescing hit rate.
- **Auth:** the API has no authentication. Keep it on localhost or behind a reverse proxy with auth
  if you expose it.
- **Keys:** keep them in `.env` (git-ignored). They're never returned by any endpoint.
- **Docker** (example):
  ```dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  COPY . .
  RUN pip install --no-cache-dir ".[yfinance,fast]"
  EXPOSE 8000
  CMD ["uvicorn", "abg.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
  ```

## 7.3 Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| `No provider could serve history … (yahoo: HTTP 429 …; stooq: …)` | Free sources throttled or blocked on your network. Add any keyed vendor to `.env`, wait a few minutes (the breakers reopen on their own), or check `abg providers --probe AAPL`. |
| `(no enabled provider supports this …)` | Nothing configured for that capability, e.g. options without Yahoo/yfinance. Enable one, or use `--demo` offline. |
| Report says "serving cached data N s old" | All vendors failed; you're seeing the last good copy. Check `abg providers`. |
| Options show "MODEL chain" | No vendor returned a chain (common for non-US tickers, or when Yahoo's crumb flow is blocked). `pip install yfinance` adds a second options source. |
| Yahoo options/fundamentals `AuthError` | Yahoo changed its cookie/crumb flow. yfinance usually adapts quickly: `pip install -U yfinance`. |
| AI insight says "rule-based" | No `ANTHROPIC_API_KEY`, API error or timeout (see the `note` field). Check the model id in `ABG_ANTHROPIC_MODEL`. |
| Alpha Vantage stops working mid-day | Daily free quota. The breaker parks it and other vendors take over. |
| Stale or odd data after changing keys | `abg cache clear` |
| Want to see every provider attempt | `abg --log-level DEBUG analyze AAPL`, or inspect `provenance[].attempts` |

## 7.4 Performance profile

Measured on the sandbox (synthetic provider, ~570 daily bars). Network time depends on the vendor.

| Stage | Cold | Warm cache |
|---|---|---|
| fetch (6 concurrent requests) | = slowest successful vendor (typically 150–600 ms live) | ~1 ms |
| compute (indicators, signal, stats, options, features) | ~50–90 ms | same |
| risk (baseline) | ~2 ms | same |
| insight | 2–8 s with Claude (then cached 1 h); ~0 ms rule-based | ~0 ms |

Tips: keep one long-running `abg serve` so the pools and caches stay warm; use `--no-ai` for
bulk runs; `compare` and `analyze_many` run concurrently (4 at a time).

## 7.5 Testing

```bash
pip install -e ".[dev]"
pytest -q
```

139 tests, all offline:

| File | Covers |
|---|---|
| `test_indicators.py` | RSI vs textbook loop, CCI vs naive, bounds, MACD/BB identities, signals & level ordering, tiny-input robustness |
| `test_options.py` | Put-call parity, Hull reference prices, Greeks vs finite differences, IV round-trip (vectorised calls and puts), arbitrage rejection, chain analytics, max pain |
| `test_resilience.py` | Breaker state machine, retry policy, Retry-After handling, token bucket, health, cache fresh/stale/persistence/corruption |
| `test_router.py` | Failover, single-flight, hedging latency, synthetic never hedged, stale-if-error, error taxonomy → breaker, validation failover, adapter-bug containment, allow-list, demo-cache leak guard |
| `test_providers.py` | Every adapter against recorded payload shapes (MockTransport), vendor throttle bodies, HTTP status mapping, timeouts, 4 CSV formats |
| `test_forecast.py` | Simulation reproducibility, ordering, analytic mean and vol checks, tilt direction, barrier odds, calibration coverage, every recommendation guard-rail, engine/API/monitor integration |
| `test_portfolio_live.py` | Average-cost/realized P&L, oversell & delete guards, backup round-trip, persistence, NYSE holidays, live-bar splicing, every signal family incl. cooldowns/bands/one-shot rules, Discord (429 retry), email digest vs critical, hub isolation, monitor sweeps + single-instance lock + poke, portfolio REST API |
| `test_extsignals.py` | Parser formats and updates, lifecycle (zone / breakout / short, approach, partials + breakeven + trailing, stop-before-target on ambiguous bars, gap fills, close-basis stops, invalidated / missed / expired, grade gate), tracker ingest/defaults/duplicates/rejections/source updates/market entries, daily-bar catch-up, Discord poller + webhook relay (MockTransport), monitor integration, REST API |
| `test_engine_risk_api.py` | End-to-end strict-JSON report, partial-failure isolation, fatal history failure, CSV, compare, feature no-look-ahead, baseline monotonicity, plug-in isolation and schema checks, portfolio math, sentiment, mocked Claude (success, cache, fallback), every REST endpoint and error code |

## 7.6 Known limitations

- **Live vendor calls weren't exercised in the build sandbox** (no outbound access to market-data
  hosts). Adapters are tested against payload shapes taken from each vendor's documented or observed
  responses. Run `abg providers --probe AAPL --capability history` (and quote/news/options/fundamentals)
  once with your keys to confirm each one on your network.
- Unofficial Yahoo endpoints can change without notice. That's why yfinance and keyed vendors sit behind it.
- Free-tier entitlements (e.g. Polygon real-time, Finnhub candles, FMP endpoints) differ by plan and change
  over time. Unsupported calls surface as `AuthError` and fail over.
- Intraday history depth is vendor-limited (e.g. Yahoo: 1m ≈ 7 days, 5–30m ≈ 60 days).
- The feature-frame export uses daily bars. Intraday risk features would need a MINOR schema bump.
- VaR figures are statistical estimates from historical returns, not guarantees.

Next: [Module reference →](08-module-reference.md)

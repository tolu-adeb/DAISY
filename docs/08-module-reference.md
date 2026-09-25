# 8. Module reference (low level)

File-by-file internals, following the dependency order from the bottom up. About 6,000 lines in total,
including the dashboard.

```
abg/
├── errors.py        exception taxonomy
├── utils.py         symbols, periods, number parsing, JSON sanitising, timers
├── config.py        Settings (pydantic-settings)
├── models.py        provider-agnostic data models + OHLCV sanitiser
├── resilience.py    retry, circuit breaker, rate limiter, health stats
├── cache.py         memory LRU + SQLite tiered cache
├── http.py          pooled async HTTP client + error mapping
├── providers/
│   ├── base.py      Provider ABC + registry
│   ├── router.py    ProviderRouter (the failover engine) + provider discovery
│   ├── yahoo.py     Yahoo direct
│   ├── keyed.py     Polygon, Alpha Vantage, Finnhub, Tiingo, FMP, Twelve Data
│   ├── free.py      yfinance, Stooq
│   └── local.py     CSV parsing/provider, synthetic generator/provider
├── analysis/
│   ├── indicators.py  signals.py  stats.py  sentiment.py  options.py  insights.py
│   └── forecast.py    Monte Carlo prediction, calibration, confidence, recommendation, thesis
├── risk/
│   ├── features.py  interface.py  baseline.py  portfolio.py
├── portfolio/
│   ├── store.py     PortfolioStore (SQLite): transactions, positions, watchlist, rules, signals
│   └── valuation.py live snapshot + portfolio risk
├── live/
│   ├── market_hours.py  signals.py  notify.py  monitor.py
├── cli_portfolio.py portfolio / alert / signals / monitor / notify-test commands
├── engine.py        AnalysisEngine orchestrator (+ splice_quote for live bars)
├── render.py        Rich terminal rendering
├── cli.py           Typer CLI
└── api/
    ├── server.py    FastAPI app (lifespan owns engine, store, hub, monitor)
    ├── portfolio_routes.py  portfolio / alerts / signals / monitor REST + SSE
    └── static/      index.html, app.css, app.js, portfolio.js, vendor/lightweight-charts.js (Apache-2.0)
```

---

## `errors.py`

| Class | `code` | retryable | counts_against_health | Raised when |
|---|---|---|---|---|
| `ABGError` | abg_error | – | – | base; `.message`, `.context`, `.to_dict()` |
| `ConfigError` | config_error | | | unknown/unconfigured forced provider |
| `InvalidSymbolError` | invalid_symbol | | | symbol fails `^[A-Z0-9][A-Z0-9.\-^=]{0,14}$` |
| `DataValidationError` | data_validation | | | sanitiser/validator rejects data |
| `ProviderError` | provider_error | ✓ | ✓ | generic vendor failure; `.provider` |
| `ProviderTimeout` | provider_timeout | ✓ | ✓ | HTTP or call timeout |
| `RateLimited` | rate_limited | ✓ | ✓* | 429 / throttle body / local bucket (*local: ✗, non-retryable) ; `.retry_after` |
| `AuthError` | auth_error | ✗ | ✓ | 401/402/403, premium-only endpoint |
| `NotSupported` | not_supported | ✗ | ✗ | capability not implemented |
| `NoDataError` | no_data | ✗ | ✗ | unknown symbol / empty range / 404 |
| `CircuitOpenError` | circuit_open | ✗ | ✗ | recorded in attempts when a breaker skips a provider |
| `AllProvidersFailed` | all_providers_failed | – | – | router exhausted; `.errors` = `[{provider, error, code}]` |

## `utils.py`

- `normalize_symbol(s)`: strip, upper-case, regex-validate (this also prevents URL-path injection).
- `period_to_start(period, end)`: `"6mo" | "1y" | "ytd" | "max" | "45d" | "2wk"` → date (month = 31 d, year = 366 d, so the window is generous).
- `is_intraday(interval)`, `INTERVALS`, `PERIOD_CHOICES`.
- `to_float(x)`: lenient parse: `None`, `"N/A"`, `"$1,234"`, `"12%"`, `{"raw": x}` (Yahoo) → float or None; NaN/inf → None.
- `jsonable(obj)`: recursive conversion of dataclasses, numpy, pandas, datetimes and enums to strict JSON (NaN/inf → None).
- `Timer`: `with timer.stage("fetch"):` records milliseconds; `total_ms()`.

## `config.py`

`Settings(BaseSettings)` with `env_prefix="ABG_"`, `.env` support and `extra="ignore"`. The Anthropic key
also accepts plain `ANTHROPIC_API_KEY` (via `AliasChoices`). Helpers: `provider_list()` (order minus disabled)
and `extra_risk_models()`. `get_settings()` is an `lru_cache` singleton for library use; the CLI and API
build their own `Settings` so flags apply.

## `models.py`

- `Capability` enum: history, quote, news, options, fundamentals.
- `PriceHistory(symbol, df, interval, source, synthetic, currency, meta)`
  - `from_frame(...)` → runs `sanitize`.
  - `sanitize(df, source)`: see [Data layer §3.5](03-data-layer.md#35-data-validation-modelspricehistorysanitize).
  - `last_close`, `last_date`, `tail(n)`, `since(date)`, `to_records()`.
- `Quote`: `__post_init__` rejects non-finite or ≤ 0 prices and derives `change` / `change_pct` from `prev_close` when missing.
- `NewsItem`: `provider_sentiment` (vendor) and `sentiment` (filled by the model).
- `OptionContract`: `.mid` = (bid+ask)/2 if the quote is valid, else last. `OptionChain.to_frame()`; `model_generated` flag.
- `Fundamentals.merge(other)`: fills missing fields from another vendor (utility for multi-vendor enrichment).
- `Provenance(capability, symbol, provider, latency_ms, cache, age_s, attempts, fetched_at)`; `Fetched[T](value, provenance)`.

## `resilience.py`

- `retry_async(fn, attempts, base_delay, max_delay)`: retries only `ProviderError.retryable`. Uses full-jitter backoff `U(0, min(max, base·2^i))`. A `RateLimited` with `retry_after ≤ max_delay` sleeps exactly that long; a longer one re-raises at once so the router fails over.
- `CircuitBreaker(failure_threshold, cooldown, clock)`: `allow()` (moves OPEN→HALF_OPEN after the cooldown and grants one probe), `record_success()` (CLOSED, cooldown reset), `record_failure()` (a HALF_OPEN failure doubles the cooldown, up to 16×), `trip(cooldown)`, `release_probe()` (a cancelled hedge doesn't consume the probe), `snapshot()`. The injectable `clock` makes it testable.
- `RateLimiter(rate, per)`: token bucket guarded by an `asyncio.Lock`. `acquire(max_wait)` waits for a token if it's due within `max_wait`, otherwise raises a local `RateLimited` (non-retryable, not a health failure).
- `HealthStats`: EWMA latency and success rate (α = 0.3). `degraded` = ≥ 4 calls and success < 0.5.

## `cache.py`

- `MemoryCache`: `OrderedDict` + `threading.Lock`; values stored as `(stored_at, value)`; LRU eviction.
- `DiskCache`: SQLite table `kv(k PRIMARY KEY, t REAL, v BLOB)`, WAL, `synchronous=NORMAL`, pickled values. Every operation is wrapped so I/O or corruption errors disable the tier instead of raising.
- `TieredCache.get(key, ttl, max_stale)` → `CacheHit(value, stored_at, fresh)` or None. A disk hit is promoted to memory. `set(key, value, persist)`, `clear(prefix)`, `stats()`.

## `http.py`

`HttpClient(timeout, connect_timeout, user_agent, max_connections, transport=None)`. The `transport` hook
is how tests inject `httpx.MockTransport`. Methods: `request`, `get_json`, `get_text`, `post_json`, `aclose`, and `raw`
(the underlying client, used for Yahoo's cookie priming). Status mapping is in [§3.4](03-data-layer.md#34-http-client-httppy).

## `providers/base.py`

`Provider` class attributes: `name`, `label`, `capabilities`, `requires_key`, `key_setting`, `rate_limit`,
`intraday`, `notes`, and optionally `call_timeout`. Methods: `is_configured()` (key present or not required;
yfinance/csv/synthetic override it), `supports(cap, interval)` (rejects intraday unless `intraday`),
`map_symbol()`, the five `get_*` coroutines (default `NotSupported`), `aclose()` and `describe()`.
`PROVIDER_CLASSES` is the registry populated by `@register_provider`.

## `providers/router.py`

- `build_providers(settings, http)`: imports the built-in adapter modules (which registers them), loads `abg.providers` entry points (a broken plug-in only logs a warning), and instantiates every class.
- `ProviderSlot(provider, breaker, limiter, health, rank)`.
- `ProviderRouter`
  - `candidates(cap, interval, only)`: forced `only` validates the provider exists, is configured and supports the capability (else `ConfigError`); otherwise it filters to allowed, configured, supporting providers sorted by `(synthetic, degraded, rank)`.
  - `_usable(provider, only)`: the guard for cached data (provider must still be configured and allowed).
  - `fetch(...)`: cache → single-flight (`asyncio.shield` on a shared task, so a cancelled caller doesn't cancel the others) → `_fetch_uncached`.
  - `_fetch_uncached`: `_race`, then cache the result; on `AllProvidersFailed`, fall back to stale cache.
  - `_race(cap, symbol, cands, call, validate)`: the hedged loop (see [§3.2](03-data-layer.md#32-the-router-algorithm-providersrouterpy)). `launch(hedging=True)` never starts `synthetic`. Pending tasks are cancelled in `finally`.
  - `_attempt(slot, call, validate)`: limiter → `wait_for(call, call_timeout)` → `retry_async` → `validate`. Maps `asyncio.TimeoutError` to `ProviderTimeout`, `DataValidationError` to `ProviderError`, and any other exception to a non-retryable `ProviderError` (logged with traceback). Records health and breaker state.
  - `_record_failure`: non-health errors reset or release a half-open probe; `AuthError` → `trip(10×cooldown)`; `RateLimited(retry_after)` → `trip(retry_after)`; everything else → `record_failure()`.
  - `status()`, `probe(symbol, cap)`: direct parallel calls to every configured provider, bypassing cache and failover.

## `providers/yahoo.py`

Endpoints and the crumb flow are described in the module docstring. `_get_crumb` does a double-checked
lock, primes cookies at `fc.yahoo.com`, calls `GET /v1/test/getcrumb`, and caches the crumb for 1 h.
`_crumbed_json` retries once with a fresh crumb on `AuthError`. `_chart` raises `NoDataError` for
`chart.error`. Intraday ranges are clamped to Yahoo's limits (1m: 7 d, 5–30m: 59 d, 60m: 729 d).
Daily indices are normalised to midnight. `BRK.B` → `BRK-B`.

## `providers/keyed.py`

One class per vendor, each with a private `_get`/`_q` that injects the key (query param or header) and
converts HTTP-200 error bodies into typed exceptions. Parsing details worth knowing:

- Polygon aggregates: ms epoch → New-York session date for daily bars. News `insights[].sentiment` maps to ±0.6.
- Alpha Vantage: `"1. open"`-style keys are stripped. `outputsize=full` automatically downgrades to `compact` if it's premium on your plan. `time_published` is parsed from `%Y%m%dT%H%M%S`.
- Finnhub: `marketCapitalization` and `shareOutstanding` are in **millions** (× 1e6); dividend yield is in % (÷ 100).
- Tiingo: uses the `Authorization: Token` header and `resampleFreq` for weekly/monthly.
- FMP: `/stable` endpoints; accepts both the list shape and the legacy `{"historical": [...]}` shape.
- Twelve Data: maps `status:error` codes 429 / 401 / 403 / 400 / 404.

## `providers/free.py`

- `YFinanceProvider`: every call goes through `_run` (thread + exception mapping; yfinance rate-limit exceptions → `RateLimited`). News handles both the old flat and the new `content`-nested shapes. `get_options` picks the requested expiry if listed, else the nearest one. yfinance's own logging is silenced.
- `StooqProvider`: rejects non-CSV bodies (quota or HTML pages) as `RateLimited` / `NoDataError`.

## `providers/local.py`

- `_num(series)`: `$`, commas, `(neg)`, K/M/B/T suffixes.
- `detect_csv_format(text)` and `parse_price_csv(text, symbol, source)`: header scan (first 60 lines), alias mapping, `format="mixed"` date parsing, then `PriceHistory` sanitising.
- `CSVProvider`: file lookup `SYMBOL.csv` / `SYMBOL_*.csv` / `SYMBOL-*.csv` / `SYMBOL *.csv` (newest wins). Weekly/monthly are resampled.
- `synthetic_frame(symbol, start, end)`: seeded by CRC32(symbol) and generated from a fixed 2000-01-03 origin, so prices are stable across calls. GBM with regime-switching vol (0.7/1.0/1.6× in 60-day blocks), Student-t(4) shocks, overnight gaps and volume that correlates with |return|.
- `SyntheticProvider`: only configured when `allow_synthetic`. Sets `synthetic=True` on everything it returns.

## `analysis/*`

Formulas are in [Analytics](04-analytics.md). Implementation notes:

- `indicators.cci` uses `numpy.lib.stride_tricks.sliding_window_view` for the mean absolute deviation (no Python loop).
- `indicators.compute_all(df, intraday)` returns a copy with ~45 columns. `latest_snapshot` picks the labelled subset.
- `signals._v` treats NaN as missing, so every component degrades gracefully on short histories.
- `signals.classify_plays` → list of `Play.to_dict()`, sorted by confidence, never empty.
- `stats.summary` accepts an optional benchmark series and aligns it by date (inner join).
- `options._is_call` accepts `"call"/"put"` strings or boolean arrays. Every function broadcasts over arrays.
- `options.implied_vol` iterates at most 60 times. Convergence is checked only on valid entries.
- `sentiment._model` is module-level state (swap it with `set_sentiment_model`).
- `insights.claude_insight(report, settings, http, cache)` posts to `/v1/messages` with headers `x-api-key` and `anthropic-version: 2023-06-01`.

## `risk/*`

- `features.FEATURES`: the list of `FeatureSpec(name, group, unit, description, timeseries)`. `FEATURE_NAMES` / `TIMESERIES_FEATURES` give the orders.
- `features._rolling_var_cvar` / `_rolling_max_drawdown`: exact windowed computations with `np.partition` / `np.maximum.accumulate`, O(n·w). That's about 5 ms for 600 bars.
- `features.build_feature_frame(ind, bench_close, ppy)` → DataFrame[TIMESERIES_FEATURES], with inf replaced by NaN.
- `features.build_features(...)` → `FeatureVector`, with values in schema order. Options features only come from market chains.
- `interface.discover(extra_paths)`: always registers `BaselineRiskModel`; loads entry points once; then `ABG_RISK_MODELS` paths (adding the CWD to `sys.path`, because console scripts don't).
- `interface.run_models`: `asyncio.gather` over the models. Sync `assess` runs via `to_thread`, async ones are awaited; there's a 10 s timeout, the score is clamped to 0–100, and baseline comes first.
- `baseline.BaselineRiskModel.BANDS`: `{dimension: (feature, weight, [(x, sub_score)…])}`, interpolated with `np.interp` (clamped at the ends).
- `portfolio.portfolio_risk`: inner-joins returns (drops NaN rows); needs ≥ 30 observations.

## `engine.py`

- `AnalyzeOptions`: every switch for one analysis (period, interval, source, section toggles, expiry, include_series, position, use_cache).
- `AnalysisEngine.__init__`: builds `HttpClient`, `TieredCache`, providers and `ProviderRouter`, then runs `risk.discover`. Everything is injectable for tests.
- `_window(period, interval, end)`: warm-up plus bucket snapping.
- `history / quote / news / fundamentals / option_chain`: thin wrappers around `router.fetch` with per-capability TTL and persistence.
- `analyze`: fetch stage (the history task plus guarded side tasks), then `_build_report`.
- `_build_report`: compute in a thread (`_compute`), then risk models, then insight (with a timeout of `ai_timeout + 5`). Attaches provenance, warnings and timings, and returns `jsonable(report)`.
- `_compute`: pure pipeline. Synthesises a quote from the last bar if the quote fetch failed. Builds the model option chain when no market chain exists. Adds staleness and demo warnings.
- `analyze_many` (semaphore), `compare` (+ `portfolio_risk`), `feature_frame` (+ labels), `status`.
- `series_payload(view)`: column-oriented chart arrays with unix-second timestamps.

## `render.py` / `cli.py`

`render.py` holds pure Rich rendering functions (report, chain, compare, providers) that adapt to terminal
width. `cli.py` is the Typer app: global flags are stored in `_state["overrides"]` and applied to `Settings`,
and `_run` owns the engine lifecycle and turns `ABGError` into a clean message and exit code 2. The spinner and logging go to
`err_console` (stderr).

## `api/server.py` and `api/static/`

- `lifespan` creates and closes one engine. GZip and CORS middleware. Exception handlers map `ABGError` subclasses to 400/422/502/500, plus `ValueError`→400, `TimeoutError`→504 and a catch-all 500.
- `_t(coro)` wraps every handler in a 60 s timeout.
- The dashboard is plain JS (`app.js`, ~440 lines). `api()` wraps fetch and surfaces `attempts[]` in a toast. Each `render*` function owns one card. `renderCharts` creates three synced Lightweight-Charts instances, re-created on theme change so they pick up the CSS tokens. All interpolated text goes through `esc()`. `safeLocale()` guards against invalid `navigator.language` tags. Theme choice is stored in `localStorage` (inside try/catch).
- `app.css`: colour tokens on `:root` with separate dark steps (OS preference and manual toggle). Categorical series colours come from a CVD-validated palette. Status colours (good/warning/serious/critical) are reserved for risk levels and always paired with a text label.

## `tests/`

`conftest.py` provides `settings` (offline, synthetic, no hedging, no retries), an `ohlcv` fixture and
`FakeProvider` (scriptable delay, failure and call counting) plus `make_router`. See [§7.5](07-operations.md#75-testing).

## `portfolio/*`, `live/*`

See [Portfolio & live signals §9.6](09-portfolio-and-live-signals.md#96-internals). Key contracts:

- `PortfolioStore.add_transaction(portfolio, symbol, side, shares, price, fees=0, ts=None, notes=None)` validates
  the whole history with `compute_positions` before inserting a sell.
- `SignalEngine._transition(symbol, key, value)` → `(changed, old)` persisted in `signal_state`, and
  `_cooled(symbol, kind, key)` looks at the last saved signal with that key.
- `NotificationHub.publish(signal)` → `store.save_signal` → `broadcast("signal")` → background `_deliver` per channel
  (60 s timeout) → `store.mark_delivered`.
- `Monitor.run()` loop: acquire lock → (analysis sweep if due, else quote sweep) → heartbeat → wait for the next
  timer or `poke()`. `poke()` during a sweep is remembered (`_poked*` flags), so bursts of edits aren't lost.
  Symbols without a recent analysis are analysed on the next quote sweep, and failed analyses back off for one
  analysis interval.

## `analysis/forecast.py`

- `simulate(close, *, symbol, rf, beta, signal_score, sentiment, plays, cfg, seed)` → forecast dict (no
  recommendation). It's pure and deterministic for a given seed. The default seed is the CRC32 of symbol, last bar
  timestamp and last close.
- `ewma_var(r, λ)` gives causal EWMA variance. `vol_term_structure(v_short, v_long, H, half_life)` gives per-day variance.
- `calibrate(r, ev, cfg, h=21)` runs the walk-forward interval coverage test (it uses only data available at each origin).
- `confidence(fc, n_bars, signal_score, primary)` is the weighted component score.
- `finalize(report, rf)` adds `recommendation` and `thesis`. The engine calls it after the risk models so the
  risk-level cap can apply.
- `ForecastConfig` holds paths, primary horizon, fan length, equity premium, tilts, Student-t ν, EWMA λ, vol
  half-life, block length and calibration origins.
- Engine hook: `AnalysisEngine._forecast` runs in the compute thread (daily interval only). `_build_report`
  calls `finalize` after risk. The monitor asks for 2,000 paths and records `recommendation`,
  `forecast_confidence` and `prob_up` per symbol.

## `extsignals/*`

See [External signals](11-external-signals.md). Key contracts:

- `parser.parse(text) -> ParsedSignal` is deterministic and does no network calls. `kind` is `idea | update | none`, and
  `trackable` needs a symbol, direction and entry type. `looks_like_signal(text)` is the cheap chat pre-filter.
- `lifecycle.step(idea, Obs, approach_pct, move_stop_to_be, allow_entry) -> [event]` is a pure state machine that
  mutates the idea. `trigger_fill(idea, obs)` gives the fill price or None. `manual_exit` handles source/user exits, and
  `summary_stats(ideas)` builds the track record (rejected ideas are excluded).
- `commentary.facts(idea, report, barrier, price)` extracts the live context. `grade(idea, facts)` returns
  `(A-D, score, reasons)`, and `explain(kind, idea, event, facts, extra)` returns `{title, summary, why, plan, risks, fields}`.
- `store.ExtSignalStore` works on `signals.sqlite3` (ideas / events / messages / kv). It is thread-safe (RLock) and uses WAL.
- `tracker.ExtSignalTracker(engine, store, hub=None, relay=None)` provides `ingest(text, source, channel_id, author,
  message_id, reply_to)`, `on_quotes(quotes)`, `review()`, `catch_up()`, `cancel/close/edit`, `view(idea)` and `stats()`.
  Mutations are serialised with an asyncio lock. Relays run in background tasks, in order, behind a second lock.
  Context (analysis + closes) is cached per symbol, and `barrier()` runs `forecast.simulate` on the idea's own levels.
- `discord.DiscordPoller.poll_once()` uses REST + saved cursors. `DiscordRelay.send(idea, kind, x) -> (status, message_id)`
  posts via a webhook or as a bot reply. `message_text(m)` flattens content, embeds and forwarded snapshots.
- `Monitor` builds the tracker and poller when `ABG_EXT_ENABLED`. It runs `catch_up` at start and starts the poller
  task inside the lock. It adds ext symbols to quote sweeps and calls `review()` on full analysis sweeps.
  `NotificationHub.publish(sig, skip={"discord"})` avoids a double Discord post when the relay is on.

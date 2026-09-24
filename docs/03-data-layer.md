# 3. Data layer

The data layer turns "give me AAPL's daily history" into a fast, reliable answer, whichever
vendors happen to be up, configured or rate-limited at that moment.

## 3.1 Providers

| # | Name (`source=`) | Key env var | History | Quote | News | Options | Fundamentals | Intraday | Local rate budget | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `yahoo` | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 8/s | Direct JSON endpoints; options & fundamentals use a cookie+crumb session, fetched lazily and refreshed on 401 |
| 2 | `polygon` | `ABG_POLYGON_API_KEY` | ✓ | ✓ (prev. session) | ✓ + sentiment | – | ✓ | ✓ | 5/min | Polygon rebranded as Massive; override base with `ABG_POLYGON_BASE_URL` |
| 3 | `tiingo` | `ABG_TIINGO_API_KEY` | ✓ (split+div adjusted) | ✓ (IEX, `tngoLast` fallback) | ✓ (paid add-on; `ABG_TIINGO_NEWS_ENABLED`) | – | ✓ | – | 50/h | Raw Tiingo OHLC is *not* split-adjusted, so the adapter uses `adjOpen/adjHigh/adjLow/adjClose/adjVolume` |
| 4 | `fmp` | `ABG_FMP_API_KEY` | ✓ | ✓ | ✓ | – | ✓ | – | 4/s | `/stable` API; also parses the legacy v3 shape |
| 5 | `twelvedata` | `ABG_TWELVEDATA_API_KEY` | ✓ | ✓ | – | – | – | ✓ | 7/min | Free "basic" plan = 8 credits/min, 800/day. Intraday timestamps are exchange-local and converted to UTC via `meta.exchange_timezone` |
| 6 | `alphavantage` | `ABG_ALPHAVANTAGE_API_KEY` | ✓ (not split-adjusted) | ✓ | ✓ + sentiment | – | ✓ | ✓ | 5/min | Free tier is 25 calls/day; "Note"/"Information" throttle bodies are detected. Keep it last for history |
| 7 | `finnhub` | `ABG_FINNHUB_API_KEY` | – | ✓ (real-time) | ✓ (relevance-ranked) | – | ✓ (+ forward P/E, margins, PEG) | – | 55/min | Free plan = 60 calls/min; candles return 403 (premium), so history isn't claimed. Unknown tickers return `c=0` → `NoDataError`. Company news is loosely tagged, so articles naming the ticker/company are ranked first |
| 8 | `yfinance` | — (pip install) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 4/s | Library runs in a thread; maintained against Yahoo changes, so a strong 2nd line for options |
| 9 | `stooq` | — | ✓ | – | – | – | – | – | 2/s | Daily/weekly/monthly CSV; `AAPL` → `aapl.us` |
| 10 | `csv` | `ABG_CSV_DIR` | ✓ | ✓ (last bar) | – | – | – | – | – | `<dir>/<SYMBOL>*.csv`, format auto-detected |
| 11 | `synthetic` | `ABG_ALLOW_SYNTHETIC=true` / `--demo` | ✓ | ✓ | – | – | – | – | – | Deterministic simulated data, always flagged, always last, never used as a hedge |

Vendor free-tier limits change often. The budgets above are conservative defaults
(`Provider.rate_limit`). If a vendor rejects a request because of your plan, that shows up as
`AuthError` and the breaker parks that vendor.

Adapters for Finnhub, Twelve Data and Tiingo were checked against live responses on 2026-09-24,
and those payloads are used as test fixtures.

**Per-capability order.** `ABG_QUOTE_ORDER`, `ABG_HISTORY_ORDER`, `ABG_NEWS_ORDER`,
`ABG_OPTIONS_ORDER` and `ABG_FUNDAMENTALS_ORDER` override the global order for that one capability
(priority *and* allow-list). For example, real-time Finnhub can win for quotes while adjusted Tiingo
wins for history.

**`ABG_PROVIDER_ORDER` sets both priority and the allow-list.** Providers not named there are
never used automatically. They can still be forced with `--source name`. The default order
favours key-less sources first for latency, then keyed vendors, then fallbacks.

## 3.2 The router algorithm (`providers/router.py`)

```
fetch(capability, symbol, call, key, ttl, only=None, validate=None)
 1. cache_key = f"{capability}:{symbol}:{key}:{only or '*'}"
 2. CACHE:      fresh hit whose provider is still allowed → return (provenance.cache="fresh")
 3. COALESCE:   identical request in flight → await it     (provenance.cache="coalesced")
 4. RANK:       candidates = configured ∧ supports(capability, interval) ∧ in allow-list
                sort by (is_synthetic, health.degraded, rank)
 5. RACE:       launch candidate[0]
                loop:
                  wait for first completion, with timeout = hedge_delay if more candidates remain
                  timeout → HEDGE: launch next candidate (never synthetic)
                  success → cancel the rest, return
                  failure → record attempt, launch next candidate immediately
 6. PROTECT (per attempt):  breaker.allow()? → token bucket (≤1 s wait, else RateLimited)
                → asyncio.wait_for(call, http_timeout×1.5) → retry_async(1 + max_retries)
                → validate(result)
 7. SUCCESS:    cache.set, provenance(provider, latency, attempts=[failed ones])
 8. ALL FAILED: stale cache ≤ max_stale whose provider is still allowed → return (cache="stale")
                else raise AllProvidersFailed(capability, symbol, attempts)
```

### Health and circuit breakers (`resilience.py`)

Each provider slot has:

- **CircuitBreaker**, with states closed → open (after `breaker_failures`=3 consecutive
  failures) → half-open (one probe after `breaker_cooldown`=60 s). A failed probe doubles
  the cooldown, up to 16×. `AuthError` trips it immediately for 10× the cooldown.
  `RateLimited(retry_after)` trips it for `retry_after` seconds.
- **HealthStats**, which tracks an EWMA (α = 0.3) of latency and success. A provider with ≥ 4 calls and a
  success EWMA below 0.5 is **degraded** and moves behind healthy providers.
- **RateLimiter**, a token bucket at the provider's budget. If the next slot is more than 1 s away
  it raises a local `RateLimited` that doesn't count against health, and the router moves on
  instead of stalling.

Errors that say nothing about a vendor's health (`NoDataError` for an unknown ticker,
`NotSupported`, local rate limits) do **not** count toward the breaker.

### Hedging, in numbers

If Yahoo normally answers in 150 ms but occasionally hangs for 8 s, then with
`hedge_delay = 1.5` the worst case becomes about 1.5 s plus the second vendor's latency, and
the typical case is unchanged. Set `ABG_HEDGE_DELAY=0` to disable hedging (strictly
sequential failover), for example to conserve a tight API quota.

## 3.3 Caching (`cache.py`)

| Tier | Implementation | Latency | Scope |
|---|---|---|---|
| Memory | `OrderedDict` LRU, 512 entries, thread-safe | µs | process |
| Disk | SQLite (WAL mode), pickled values | ~1 ms | survives restarts (`~/.cache/abg-terminal/cache.sqlite3`) |

Entries store **write time**. Each read passes a TTL:

| Data | TTL (setting) | Persisted to disk |
|---|---|---|
| Quote | 15 s (`ttl_quote`) | no |
| Intraday history | 60 s | yes |
| Daily history | 15 min | yes |
| News | 10 min | yes |
| Options chain | 2 min | no |
| Fundamentals | 24 h | yes |
| Claude insight | 1 h (keyed by digest hash) | yes |

Stale-if-error allows any persisted entry up to `max_stale` (7 days) to be served when every
vendor fails. Cached data is only served if the provider that produced it is **still allowed**,
so synthetic data from a `--demo` run can never leak into a normal run.

CLI: `abg cache stats`, `abg cache clear [prefix]`. API: `?refresh=true` bypasses the cache.

## 3.4 HTTP client (`http.py`)

One `httpx.AsyncClient` is shared by every adapter: keep-alive pool (32), connect timeout 4 s,
total 8 s, redirects followed, and the proxy taken from the environment (`HTTPS_PROXY`). Errors
are mapped as follows:

| Condition | Raised | Retryable | Counts against health |
|---|---|---|---|
| timeout | `ProviderTimeout` | ✓ | ✓ |
| connection error | `ProviderError` | ✓ | ✓ |
| 429 | `RateLimited(retry_after)` | ✓ (if short) | ✓ (breaker tripped for retry_after) |
| 401 / 402 / 403 | `AuthError` | ✗ | ✓ (long trip) |
| 404 | `NoDataError` | ✗ | ✗ |
| 5xx | `ProviderError` | ✓ | ✓ |
| other 4xx | `ProviderError` | ✗ | ✓ |
| non-JSON body | `ProviderError` | ✓ | ✓ |

Vendors that return HTTP 200 with an error body (Alpha Vantage "Note", Twelve Data
`status:error`, Polygon `status:ERROR`, FMP "Error Message") are detected inside the adapter
and mapped to the same exceptions.

## 3.5 Data validation (`models.PriceHistory.sanitize`)

Every history passes through one choke point, which does the following:

- lower-cases the columns, coerces them to float64, converts the index to UTC and drops the timezone
- drops rows with NaN or non-positive closes and removes duplicate timestamps (keeping the last)
- fills missing open/high/low from close, and sets high/low to the max/min of OHLC (repairs vendor glitches)
- clips negative volume to 0 and sorts ascending
- raises `DataValidationError` if nothing usable remains, which the router treats as a provider failure

The engine also requires at least 2 bars, and warns if the last daily bar is more than 5 days old.

## 3.6 CSV auto-detection (`providers/local.py`)

`parse_price_csv(text)` handles:

| Format | Signature | Quirks handled |
|---|---|---|
| MacroTrends | "macrotrends" in header junk | Skips disclaimer lines until a `date,…close` header |
| Yahoo | `Adj Close` column | `adj_close` retained |
| Nasdaq | `Close/Last` | `$` prefixes, newest-first order |
| Investing.com | `Vol.` and `Change %` | `Price` → close, `58.41M` suffixes |
| Generic | anything with a date and close/price column | aliases: datetime, timestamp, last, adjusted close… |

Numbers accept `$`, thousands commas, `(negative)` notation and K/M/B/T suffixes.

## 3.7 Adding a provider

See `examples/custom_provider.py`. In short:

```python
class MyVendor(Provider):
    name = "myvendor"; capabilities = frozenset({Capability.HISTORY, Capability.QUOTE})
    requires_key = True; key_setting = "myvendor_api_key"; rate_limit = (5, 1.0)
    async def get_history(self, symbol, start, end, interval="1d") -> PriceHistory: ...
    async def get_quote(self, symbol) -> Quote: ...
```

Rules: use `self.http` (so you inherit pooling and error mapping), return the models from
`abg.models`, and raise `NoDataError` / `AuthError` / `RateLimited` rather than returning `None`.
Then register it in one of three ways: `@register_provider` (built-ins), `AnalysisEngine(providers=[...])`,
or the `abg.providers` entry-point group, and add its name to `ABG_PROVIDER_ORDER`.

Next: [Analytics →](04-analytics.md)

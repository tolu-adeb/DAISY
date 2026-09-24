# 2. Architecture

## Layers

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  INTERFACES      cli.py (Typer+Rich)   api/server.py (FastAPI)   Python lib  │
│                  render.py             api/static/ (dashboard)               │
├──────────────────────────────────────────────────────────────────────────────┤
│  ORCHESTRATION   engine.py  AnalysisEngine                                   │
│                  fetch (async, concurrent) → compute (thread) → risk → AI    │
├───────────────────────────────┬──────────────────────────────────────────────┤
│  ANALYTICS (pure functions)   │  RISK                                        │
│  analysis/indicators.py       │  risk/features.py   (versioned schema)       │
│  analysis/signals.py          │  risk/interface.py  (RiskModel + registry)   │
│  analysis/stats.py            │  risk/baseline.py   (built-in model)         │
│  analysis/sentiment.py        │  risk/portfolio.py  (multi-asset)            │
│  analysis/options.py          │                                              │
│  analysis/insights.py (Claude)│                                              │
├───────────────────────────────┴──────────────────────────────────────────────┤
│  DATA            providers/router.py  ProviderRouter (cache, coalesce, rank, │
│                                       hedge, protect, validate, degrade)     │
│                  providers/yahoo.py  keyed.py  free.py  local.py  (adapters) │
├──────────────────────────────────────────────────────────────────────────────┤
│  CORE            http.py (pooled async client + error mapping)               │
│                  cache.py (memory LRU + SQLite)  resilience.py (retry,       │
│                  breaker, rate limiter, health)  models.py  errors.py        │
│                  config.py  utils.py                                         │
└──────────────────────────────────────────────────────────────────────────────┘
```

Dependencies only point **downward**. The analytics layer never performs I/O: every
function takes a DataFrame or dict and returns one, so it's trivially testable and reusable
in notebooks and backtests.

## Request lifecycle: `analyze("AAPL")`

```
AnalysisEngine.analyze
│
├─ 1. FETCH STAGE  (asyncio, all concurrent)
│     history(AAPL, window incl. warm-up)  ← the only fatal dependency
│     quote(AAPL)            ─┐
│     news(AAPL)              │ each wrapped in guard(): failure → warnings[], value None
│     fundamentals(AAPL)      │
│     option_chain(AAPL)      │
│     history(SPY) benchmark ─┘
│       each call → ProviderRouter.fetch → cache? → in-flight? → ranked, hedged race
│
├─ 2. COMPUTE STAGE  (asyncio.to_thread → event loop stays responsive for the API)
│     indicators.compute_all  → signals (score, regime, plays) → stats.summary
│     → support_resistance → sentiment.analyze_news
│     → options.analyze_chain (market chain, or theoretical_chain fallback)
│     → features.build_feature_frame / build_features (FeatureVector)
│
├─ 3. RISK STAGE  interface.run_models: every registered model concurrently,
│                 each with timeout + exception isolation + schema-version check
│
├─ 4. INSIGHT STAGE  insights.claude_insight (cached, timeout) or rule_based_insight
│
└─ 5. ASSEMBLE  JSON-safe dict + provenance[] + warnings[] + timings_ms{}
```

### Why a warm-up window?

A 200-day SMA is undefined for the first 199 bars. If you ask for a 3-month view, the
engine still fetches enough history (`warmup_days`, default 420) so that every indicator
is valid on the first displayed bar. Fetch windows snap to canonical buckets
(800 / 1900 / 3700 days / max), which means 1mo, 3mo, 6mo and 1y analyses all share **one** cached
download.

## Concurrency model

- One event loop and one `AnalysisEngine` per process. The API server shares a single
  engine across all requests, so the connection pool, cache, breakers and in-flight map are shared.
- **I/O** is async (`httpx.AsyncClient`, keep-alive pool of 32).
- **CPU work** (pandas/numpy) runs in a worker thread via `asyncio.to_thread`, so a heavy
  computation for one request doesn't delay I/O for others.
- **Sync libraries** (yfinance) are wrapped in `asyncio.to_thread` as well.
- **Single-flight:** identical concurrent requests share one upstream call.
- `analyze_many` / `compare` bound parallelism with a semaphore (default 4).

## Stability guarantees

| Failure | What happens |
|---|---|
| Vendor slow | Hedge: after `hedge_delay` (1.5 s) the next vendor starts in parallel and the first success wins |
| Vendor errors / times out | Retry once if retryable, then fail over; the circuit breaker opens after 3 consecutive failures |
| Vendor rate-limits | Local token bucket avoids most 429s; a real 429 trips the breaker for `Retry-After` seconds |
| Bad API key / plan | `AuthError` → breaker opens for 10× the cooldown; no retries wasted |
| Vendor returns garbage | `PriceHistory.sanitize` repairs or rejects it; validation failure = provider failure → failover |
| Adapter bug (e.g. vendor changed JSON) | Caught in the router, logged, converted to `ProviderError` → failover |
| Every vendor down | Stale cache served (≤ 7 days by default), flagged in `provenance.cache = "stale"` and `warnings` |
| News / options / fundamentals / benchmark fail | That section is `null`, a warning is added, and the report is still produced |
| Risk plug-in crashes / hangs | That model's entry becomes `{"error": …}`; other models are unaffected |
| Claude API down / slow | Rule-based insight with a note |
| Cache file corrupt / unwritable | Disk tier disables itself; memory tier keeps working |
| Unhandled exception in API | Global handler returns JSON 500; the worker keeps serving |

## Key design decisions

1. **Provider-agnostic models** (`models.py`). Adapters convert vendor payloads into
   `PriceHistory`, `Quote`, `NewsItem`, `OptionChain` and `Fundamentals`. Nothing above the
   data layer knows which vendor answered.
2. **Readers decide freshness.** The cache stores write-time, not expiry. The same entry
   can be "fresh" for a 15 s quote TTL and still usable as a "stale" fallback for 7 days.
3. **Explainability over black boxes.** The signal, setups and baseline risk model all return
   the reasons behind their numbers. That makes the baseline a sensible benchmark for any ML
   model you add later.
4. **Schema-versioned features.** The risk layer's input contract is explicit and
   versioned (see [Risk integration](05-risk-integration.md)).
5. **No build step for the UI.** One HTML, one CSS and one JS file, plus a vendored chart library, all
   served by FastAPI. It works offline and needs no Node toolchain.

Next: [Data layer →](03-data-layer.md)

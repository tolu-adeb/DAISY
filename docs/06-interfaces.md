# 6. Interfaces

## 6.1 CLI (`abg`)

Global options go **before** the command: `abg [--demo] [--no-cache] [--csv-dir DIR] [--providers LIST] [--log-level L] COMMAND …`

| Command | Purpose | Key options |
|---|---|---|
| `analyze SYMBOL` | Full report | `-p/--period` (1mo 3mo 6mo ytd 1y 2y 5y 10y max, or `45d`), `-i/--interval` (1d 1wk 1mo 1h 30m 15m 5m), `-s/--source`, `--ai/--no-ai`, `--news/--no-news`, `--options/--no-options`, `--expiry`, `--account` + `--risk-pct` (position sizing), `--json`, `--save FILE` |
| `compare A B C…` | Side-by-side table, correlation matrix, equal-weight portfolio risk | `-p`, `--json` |
| `quote A B…` | Latest quotes with source and cache state | `-s` |
| `watch A B…` | Live quote board | `--every SECONDS` |
| `options SYMBOL` | Chain with IV + Greeks | `--expiry`, `--strikes N` |
| `news SYMBOL` | Scored headlines | `--limit` |
| `bs` | Black-Scholes calculator / IV solver | `--spot --strike --days --vol [--rate --div --price --kind]` |
| `analyze-csv FILE` | Analyse a local CSV (format auto-detected) | `--symbol`, `-p`, `--json` |
| `providers` | Provider readiness, breaker state, health | `--probe SYMBOL`, `--capability` |
| `features A B…` | Export the feature matrix | `-p`, `--labels`, `-o file.csv/.parquet` |
| `schema` | Print the feature schema | |
| `serve` | Dashboard + REST API | `--host`, `--port`, `--reload` |
| `cache stats` / `cache clear [prefix]` | Cache maintenance | |
| `portfolio show / buy / sell / watch / unwatch / set / history / remove-tx / export / import / list` | Saved portfolio | `-P name`, `--price`, `--date`, `--fees`, `--stop`, `--target` |
| `alert add / list / remove / kinds` | Custom alert rules | `--repeat`, `--note` |
| `signals` | Signal history | `--limit`, `--symbol` |
| `monitor` | Live monitor in this window | `--once`, `--interval` |
| `notify-test` | Test Discord / email / desktop | `--channel` |
| `version` | Version | |

Exit codes: 0 ok, 1 no data for the request, 2 handled error (message plus per-provider attempts printed), 130 Ctrl-C.
Spinners and logs go to **stderr**, so `--json` output on stdout is always clean.

## 6.2 Web dashboard

`abg serve` then open http://127.0.0.1:8000 (or `/?s=NVDA` to deep-link).

- Ticker, period, interval, source (Auto = failover, or pin a vendor), toggles for AI, options and news.
- KPI row: quote and fundamentals, signal gauge with top reasons, regime, risk level and VaR.
- Price chart: candles, volume, SMA 20/50/200, Bollinger, VWAP (toggles), live OHLC legend on hover, and
  synced RSI and MACD panes. Zooming or scrolling one pane moves all three.
- Setups with conditions checklist and levels, indicator table, key levels with distance-to-price.
- Insight (bull/bear/risks), risk breakdown bars (sub-score per dimension) and extended VaR metrics.
- Options chain around ATM with an expiry selector (refetches that expiry) and a model-chain badge when applicable.
- News with per-headline sentiment, and a statistics table.
- Footer with the source and latency of every data piece, stage timings and bar counts.
- **CSV** button: upload any supported CSV for instant analysis.
- **Compare** tab: table, correlation heat-map (diverging blue/red with a neutral midpoint) and portfolio risk contributions.
- **Providers** tab: live provider status (circuit state, success rate, latency).
- Light/dark (follows the OS, with a manual toggle), usable at phone width. The chart library is
  vendored in `abg/api/static/vendor/`, so it works offline.

## 6.3 REST API

OpenAPI docs are at `/docs`. All responses are JSON. Errors are always `{"error": {"code", "message", …}}`.

| Method & path | Returns |
|---|---|
| `GET /api/health` | `{status, version}` |
| `GET /api/meta` | periods, intervals, configured providers, demo flag, AI flag |
| `GET /api/status` | providers (health/breaker), cache stats, risk models, schema version |
| `GET /api/analyze/{symbol}` | Full report. Query: `period, interval, source, ai, news, options, expiry, series (default true), refresh` |
| `GET /api/quote/{symbol}` | `{quote, provenance}` |
| `GET /api/history/{symbol}` | `{bars:[{date,open,high,low,close,volume,…}], provenance}` (no warm-up) |
| `GET /api/news/{symbol}?limit=` | `{sentiment, news[], provenance}` |
| `GET /api/options/{symbol}?expiry=` | `{options, warnings, provenance}` |
| `GET /api/risk/{symbol}` | `{risk[], features, warnings}` |
| `GET /api/compare?symbols=A,B,C&period=` | `{rows[], portfolio}` (max 12 symbols) |
| `GET /api/features/{symbol}?period=&labels=&tail=` | column-oriented feature matrix |
| `GET /api/schema` | feature schema |
| `POST /api/analyze-csv` | body `{symbol, csv, period}` → full report |
| portfolio / alerts / signals / monitor | see [Portfolio & live signals §9.5](09-portfolio-and-live-signals.md#95-api) (includes the SSE stream) |

| Status | When |
|---|---|
| 400 | invalid symbol / period / unconfigured `source` |
| 422 | data failed validation (e.g. unusable CSV) |
| 502 | every provider failed and there was no stale cache (`attempts[]` lists each vendor's error) |
| 504 | request exceeded 60 s |
| 500 | unexpected (logged; the server keeps running) |

GZip compression is on for responses over 1 KB. A full 1-year report with chart series is about 90 KB raw and about 37 KB gzipped.

## 6.4 Python library

```python
from abg import AnalysisEngine, AnalyzeOptions, Settings

async with AnalysisEngine(Settings(polygon_api_key="…")) as eng:
    report = await eng.analyze("AAPL", AnalyzeOptions(period="2y", ai=False))
    fetched = await eng.history("AAPL", "5y")          # Fetched[PriceHistory] (.value, .provenance)
    q = (await eng.quote("AAPL")).value                  # Quote
    chain = (await eng.option_chain("AAPL")).value       # OptionChain
    reports = await eng.analyze_many(["A", "B"], concurrency=4, ai=False)
    cmp = await eng.compare(["AAPL", "MSFT"], "1y")
    frame = await eng.feature_frame("AAPL", "10y", labels=True)   # pandas DataFrame
    report = await eng.analyze_csv(open("x.csv").read(), "X")
    eng.status()
```

The pure analytics are importable on their own, for example `abg.analysis.indicators.compute_all(df)`,
`abg.analysis.options.implied_vol(...)` or `abg.risk.build_feature_frame(ind)`.

## 6.5 Report JSON reference (top level)

```jsonc
{
  "symbol": "AAPL", "name": "Apple Inc.", "as_of": "2026-09-22T00:00:00", "generated_at": "…",
  "period": "1y", "interval": "1d",
  "data_quality": { "synthetic": false, "bars_total": 573, "bars_in_view": 252, "first_bar": "…",
                    "last_bar": "…", "last_bar_age_days": 1, "history_source": "yahoo", "csv_format?": "…" },
  "quote":        { "price", "prev_close", "change", "change_pct", "open", "day_high", "day_low", "volume", "market_cap", "currency", "source", … },
  "fundamentals": { "name", "sector", "industry", "market_cap", "pe", "forward_pe", "eps", "beta", "dividend_yield", "week52_high", "week52_low", … } | null,
  "signal":       { "score": 49.0, "label": "Bullish", "components": { "trend": {"vote", "weight", "reason"}, … } },
  "regime":       { "trend", "trend_strength", "adx", "volatility_regime", "bandwidth_percentile" },
  "plays":        [ { "name", "direction", "confidence", "horizon", "thesis", "levels": {entry, stop, target_1, target_2, risk_per_share, risk_pct}, "conditions": [{condition, met}] } ],
  "levels":       { "pivots": {pivot, r1, s1, r2, s2}, "resistance": [..], "support": [..] },
  "indicators":   { "rsi_14", "macd", "macd_signal", "macd_hist", "bb_pctb", "adx", "atr_14", "williams_r", "cci_20", "vwap_20", … },
  "statistics":   { "total_return_pct", "cagr_pct", "ann_volatility_pct", "sharpe", "sortino", "max_drawdown_pct", "beta", … },
  "sentiment":    { "score", "label", "articles", "positive", "negative", "neutral", "last_24h", "model", "provider_scores_used" },
  "news":         [ { "title", "url", "publisher", "published_at", "sentiment", "provider_sentiment", "source" } ],
  "options":      { "available", "source", "model_generated", "underlying_price", "expirations", "selected_expiry", "days_to_expiry",
                    "summary": { "atm_iv", "expected_move", "skew_25d", "put_call_oi_ratio", "max_pain", "term_structure" }, "contracts": [..] },
  "features":     { "schema_version": "1.0.0", "coverage": 0.8, "values": { "<40 features>" } },
  "risk":         [ { "model": "baseline", "version", "score", "level", "metrics", "drivers", "warnings" }, … ],
  "ai_insight":   { "engine", "summary", "bull_case", "bear_case", "key_levels", "risks_to_watch", "stance", "confidence", "note?" },
  "series?":      { "time": [unix…], "open", "close", "sma_20", "bb_upper", "rsi_14", "macd", … },   // API / include_series
  "provenance":   [ { "capability", "symbol", "provider", "latency_ms", "cache", "age_s", "attempts": [{provider, error, code}] } ],
  "warnings":     [ "news: …" ],
  "timings_ms":   { "fetch", "compute", "risk", "insight", "total" },
  "disclaimer":   "…"
}
```

All floats are finite or `null` (NaN and inf are never emitted), so the output is strict JSON.

Next: [Operations →](07-operations.md)

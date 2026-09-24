# 1. Overview

## What the terminal is

The ABG Intelligence Terminal is a stock-analysis engine. You give it a ticker and it returns
one structured **report** built from market data, pulled from whichever source is available:

| Report section | What it contains |
|---|---|
| `quote` | Latest price, change, day range, volume |
| `fundamentals` | Name, sector, market cap, P/E, beta, dividend yield, 52-week range (from the first vendor that answers) |
| `indicators` | Latest values of 25+ technical indicators (RSI, MACD, Bollinger, ADX/DI, ATR, OBV, Williams %R, CCI, MFI, Stochastic, VWAP, SMAs/EMAs…) |
| `signal` | Composite bullish/bearish score in −100…+100, with the vote and reason for every component |
| `regime` | Trend state (up / down / sideways), trend strength, volatility regime |
| `plays` | Rule-based trade setups (momentum breakout, trend pullback, mean reversion, fade, breakdown, squeeze) with the conditions met, confidence, entry / stop / targets |
| `levels` | Floor pivots and clustered swing support/resistance |
| `statistics` | Return, CAGR, volatility (20/60/252d), Sharpe, Sortino, Calmar, drawdowns, beta/correlation vs SPY, skew/kurtosis |
| `sentiment` + `news` | Scored headlines, recency-weighted aggregate, vendor sentiment blended in when available |
| `options` | Chain with our own implied vols and full Greeks, ATM IV, expected move, 25Δ skew, put/call ratios, max pain, term structure (falls back to a clearly-flagged Black-Scholes model chain when no market chain is available) |
| `features` | A 40-feature, schema-versioned vector, which is the input contract for risk models |
| `risk` | Output of every registered risk model (the built-in baseline gives a 0–100 score, level, VaR/CVaR, Cornish-Fisher VaR and ranked drivers) |
| `ai_insight` | Claude-written summary / bull case / bear case / risks (falls back to a deterministic rule-based write-up) |
| `provenance`, `warnings`, `timings_ms` | Which vendor served each piece and how fast, what degraded, and per-stage latency |

You can reach the same engine four ways: the `abg` **CLI**, the **web dashboard**, the **REST API**
and the **Python library**.

## Design goals (from the v3 brief)

1. **More capability.** More analytics, multi-symbol comparison with portfolio risk, CSV
   analysis, feature export and a Black-Scholes calculator.
2. **Lower latency.** Everything that can run in parallel does. Connections are pooled,
   duplicate requests are merged, slow vendors are hedged, and there are two cache tiers.
   A warm-cache analysis takes about 80–150 ms end to end, most of it computation.
3. **Stability.** No single failure can crash a run. Each vendor sits behind a timeout, a
   retry policy, a rate limiter and a circuit breaker. The router fails over to the next
   vendor, and if every vendor is down it serves recent cached data with a flag. Each report
   section is isolated, so the only fatal error is having no price history at all.
4. **Many data sources.** A provider plug-in contract with ten built-in adapters. Adding a
   vendor means writing one small class.
5. **Ready for a risk model.** A versioned feature schema, a `RiskModel` protocol, three
   ways to register a model without editing terminal code, and an exporter that produces
   leakage-free training data with forward-looking labels.

## What changed from v2 (Node.js CLI + React)

| Area | v2 | v3 |
|---|---|---|
| Language | JavaScript (shared JS engine) | Python 3.10+ (numpy/pandas vectorised maths) |
| Data | Yahoo-centric + CSV auto-detect | 10 adapters behind a failover router; CSV auto-detection kept and extended (MacroTrends, Yahoo, Nasdaq, Investing.com, generic) |
| Errors | Exceptions could end a run | Typed error taxonomy; each report section isolated; stale-if-error |
| Performance | Sequential I/O | Async concurrent I/O, hedging, single-flight, memory + SQLite cache |
| Options | Black-Scholes chain + Greeks | Plus vectorised IV solver (Newton + bisection safeguard), skew, max pain, expected move, term structure, P(ITM) |
| AI insight | Claude API | Claude API with a compact digest (fewer tokens, faster), response caching, structured JSON output, deterministic fallback |
| Risk | — | Feature schema, plug-in models, baseline VaR/CVaR model, portfolio risk, training export |
| UI | React dashboard | Dependency-free dashboard served by the API (TradingView Lightweight Charts vendored locally, so no CDN needed), light/dark, mobile-friendly |
| Quality | — | 82 offline tests |

## What it deliberately does *not* do

- **Place trades or give advice.** Every output is educational and labelled as such.
- **Invent data.** Simulated prices only appear in `--demo` mode, carry a banner and are
  never served from cache in a normal run. Model-generated option chains are flagged
  `model_generated: true`.
- **Hide degradation.** Stale data, failovers and missing sections all show up in `warnings`
  and `provenance`.

Next: [Architecture →](02-architecture.md)

# ABG Intelligence Terminal v3 (Python)

Multi-source stock analysis engine for **AI Business Group**: a rewrite of the Node.js/React
"ABG Intelligence Terminal" in Python, built for more capability, lower latency and fewer crashes.
It also has a ready-made interface for plugging in a risk-analysis model later.

```
abg analyze NVDA            # full report in the terminal
abg serve                   # web dashboard at http://127.0.0.1:8000
```

| | v2 (Node/React) | **v3 (this)** |
|---|---|---|
| Data sources | Mostly one live source + CSV | **10 adapters**: Yahoo, yfinance, Polygon/Massive, Tiingo, FMP, Twelve Data, Alpha Vantage, Finnhub, Stooq, local CSV (+ synthetic demo) |
| Failure handling | A source error could break the run | Automatic failover, retries, circuit breakers, rate limiters, stale-cache fallback; each section is isolated so one failure can't break the report |
| Latency | Sequential fetches | Concurrent fetches, pooled keep-alive HTTP, hedged requests, request coalescing, 2-tier cache (memory + SQLite) |
| Analytics | RSI, MACD, BB, ADX, OBV, W%R, CCI, VWAP, plays, sentiment, Black-Scholes, Claude insight | All of those, plus Stochastic, MFI, ATR, regime detection, scored & explained signal, trade levels, S/R clusters, Sharpe/Sortino/Calmar/beta, IV solver, skew, max pain, term structure, expected move |
| Risk | — | **Versioned feature schema (40 features)**, `RiskModel` plug-in interface, baseline VaR/CVaR model, portfolio risk, training-data export with forward labels |
| Interfaces | CLI + React | Rich CLI (14 commands), REST API with OpenAPI docs, dashboard, importable Python library |
| Portfolio & alerts | — | Saved portfolio (average-cost P&L, stops/targets, watchlist), continuous live monitor, 16 edge-triggered signal types, dashboard + desktop + Discord + email alerts |
| Tests | — | 99 offline tests (math checked against reference values, recorded API payloads, failover and error scenarios) |

## Install

```bash
cd abg-terminal
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[all]"                                # or: pip install -e .   (minimal)
cp .env.example .env                                   # optional: add any API keys you have
```

Python 3.10+. You don't need any configuration: with no keys it uses the free sources
(Yahoo, yfinance, Stooq). Each key you add to `.env` gives the router another source to fail over to.

## Use

```bash
abg analyze AAPL                         # technicals, setups, stats, options/Greeks, news, risk, AI insight
abg analyze TSLA -p 6mo -i 1d --no-ai    # period / interval / skip Claude
abg analyze MSFT --account 25000 --risk-pct 1   # adds position sizing for the top setup
abg analyze AAPL --json --save aapl.json # machine-readable report
abg compare AAPL MSFT NVDA SPY           # side-by-side + correlation + portfolio risk
abg options AAPL --expiry 2026-10-16     # chain with IV + full Greeks
abg news NVDA                            # headlines with sentiment
abg quote AAPL MSFT  |  abg watch AAPL MSFT --every 10
abg bs --spot 100 --strike 105 --days 30 --vol 30      # Black-Scholes calculator (or --price to solve IV)
abg analyze-csv ~/Downloads/AAPL_macrotrends.csv       # MacroTrends/Yahoo/Nasdaq/Investing CSVs auto-detected
abg providers --probe AAPL               # which sources work right now, and how fast
abg features AAPL MSFT -p 5y --labels -o train.csv     # risk-model training matrix
abg schema                               # feature schema
abg serve                                # dashboard + REST API + live monitor (/docs for OpenAPI)

# saved portfolio + live signals (see docs/09)
abg portfolio buy AAPL 10                # price defaults to the live quote
abg portfolio watch NVDA AMD
abg portfolio set AAPL --stop 300 --target 380
abg portfolio show                       # live P&L, weights, portfolio VaR
abg alert add NVDA price_above 150
abg monitor                              # run the live signal monitor in this window
abg notify-test                          # check Discord / email / desktop alerts
abg --demo analyze AAPL                  # offline demo with clearly-flagged simulated data
```

Library:

```python
import asyncio
from abg import AnalysisEngine

async def main():
    async with AnalysisEngine() as eng:
        r = await eng.analyze("AAPL", period="1y")
        print(r["signal"]["label"], r["risk"][0]["level"], r["plays"][0]["name"])

asyncio.run(main())
```

## Documentation (high level → low level)

1. [Overview](docs/01-overview.md): what it does and what changed from v2
2. [Architecture](docs/02-architecture.md): layers, request lifecycle, concurrency, stability guarantees
3. [Data layer](docs/03-data-layer.md): providers, the failover router, caching, resilience, error taxonomy
4. [Analytics](docs/04-analytics.md): formulas and rules for every indicator, signal, setup, statistic, sentiment and option calculation
5. [Risk integration](docs/05-risk-integration.md): feature schema, `RiskModel` interface, baseline model, training workflow
6. [Interfaces](docs/06-interfaces.md): CLI, REST API, library, report JSON reference
7. [Operations](docs/07-operations.md): configuration reference, deployment, troubleshooting, testing, limitations
8. [Module reference](docs/08-module-reference.md): file-by-file, function-by-function internals
9. [Portfolio & live signals](docs/09-portfolio-and-live-signals.md): saved portfolio, monitor, signal catalogue, Discord/email/desktop alerts

## Tests

```bash
pytest -q          # 99 tests, fully offline (no network, no API keys)
```

> Educational analysis tool, not investment advice.

# 10. Prediction: simulated future returns, thesis, confidence and recommendation

`abg predict AAPL`, the **Prediction** card in the dashboard and `report["forecast"]` in the API all come
from `abg/analysis/forecast.py`. The module simulates thousands of possible price paths and turns their
distribution into ranges, probabilities, scenarios, a written thesis, a confidence rating and a
five-level recommendation (Strong Buy / Buy / Hold / Reduce / Sell).

> **What to expect.** The simulation is good at measuring *uncertainty*: how wide the range is, how fat
> the tails are, the odds of a stop being hit before a target. It is deliberately modest about
> *direction*, because short-horizon equity drift is small compared with volatility and very hard to
> estimate. The walk-forward calibration check (below) tells you how trustworthy the ranges have been for
> that stock. Treat the output as structured educational analysis, not trade instructions.

## 10.1 Pipeline

```
daily closes (with today's live bar spliced in)
        │
        ├─ log returns r_t ─► EWMA variance (λ=0.94) ─► v_short = today's variance
        │                  └► 3-year variance        ─► v_long
        │     variance term structure  v(t) = v_long + (v_short − v_long)·e^(−t·ln2/22)
        │
        ├─ drift μ (annual) = r_f + β·ERP + signal tilt + news tilt
        │
        ├─ Model A  GBM-t:  Δlog S = μ/252 − v(t)/2 + √v(t)·ε,  ε ~ Student-t(5)/√(5/3)
        ├─ Model B  FHS:    ε = historical r_t / σ_{t|t−1}, resampled in 10-day blocks
        │                   (keeps real skew, fat tails and short-range dependence)
        │     2,500 paths each (ABG_FORECAST_PATHS=5000), deterministic seed per symbol + last bar
        ▼
 horizon stats · fan chart · scenarios · setup barrier odds · walk-forward calibration
        ▼
 confidence (0-100) ─► recommendation + thesis (after the risk models run)
```

### Drift, and why it's conservative

| Component | Formula | Typical size |
|---|---|---|
| Risk-free | `ABG_RISK_FREE_RATE` | 4% |
| Equity premium | β (clamped 0.3–2.5, default 1) × `ABG_FORECAST_EQUITY_PREMIUM` (5%) | 1.5–12% |
| Signal tilt | `ABG_FORECAST_SIGNAL_TILT` (0.25) × score/100 × long-run annual vol | at most about ±½·¼·vol, e.g. ±5% for a 40%-vol stock at score ±50 |
| News tilt | 2% × sentiment (−1…1) | ±2% max |

The historical sample mean isn't used as drift: over a few years it's dominated by noise (a stock with
30% vol needs decades of data to pin its mean return down to ±2%). Beta times a long-run premium
is the standard, far more stable prior. The technical signal only nudges it, so a strongly bullish chart
shifts the median a little but can't manufacture a big expected return.

### Volatility

Today's EWMA variance (the RiskMetrics λ = 0.94) captures volatility clustering. Forecast variance then
reverts toward the 3-year level with a 22-trading-day half-life, the shape a GARCH(1,1) forecast has.
So a calm stock after a shock gets wide near-term bands that narrow later, and vice versa.

### Two models, one ensemble

- **GBM-t** is smooth and parametric, with Student-t(5) fat tails.
- **Filtered historical simulation** replays the stock's own standardised shocks: real crash days,
  real skew, real clustering inside 10-day blocks, rescaled to today's volatility.

Half the paths come from each. If their medians disagree a lot, the "model agreement" part of confidence drops.

## 10.2 Outputs

| Field | Meaning |
|---|---|
| `horizons[]` | For 1 week, 1 month, 3 months, 6 months and 1 year: return and price percentiles (P5, P10, P25, P50, P75, P90, P95), mean, standard deviation, P(up), P(> +10%), P(< −10%), VaR95, CVaR95, and each model's median |
| `fan` | Daily P5, P25, P50, P75 and P95 prices for the next 126 trading days (the chart) |
| `scenarios` | Bear (worst 25% of outcomes), Base (middle 50%) and Bull (best 25%) at the primary horizon: average return, average price and range |
| `barriers[]` | For each directional trade setup: P(target 1 hit before the stop), P(stop first), P(neither), median days to target, P(target 2 before stop), expected R multiple |
| `calibration` | Walk-forward test: at up to 40 past origins (every 21 days, using only data available then), build the same 1-month 90% and 50% intervals and count how often the actual return landed inside. Well calibrated ≈ 90% / 50% |
| `volatility`, `drift` | Every input shown, so the result can be audited |
| `confidence` | Score (0–100), rating and components (below) |
| `recommendation` | Action, score, components, horizon, expected/excess return, P(up), risk-adjusted edge, notes, vol-target allocation and levels (entry zone, stop, P75/P90 upside, P10/P25 downside) |
| `thesis` | Headline, context, supporting points, points against, setup odds, invalidation conditions, method and disclaimer |

## 10.3 Confidence rating

| Component | Weight | Score |
|---|---|---|
| Calibration | 0.30 | 1 − (|coverage90 − 0.90| + ½·|coverage50 − 0.50|) / 0.30 |
| Data history | 0.15 | bars / 750 (capped at 1) |
| Model agreement | 0.15 | exp(−|median_GBM − median_FHS| / (0.25·σ_h)) |
| Directional edge | 0.15 | |P(up) − 0.5| / 0.15 |
| Signal clarity | 0.15 | |score| / 60 |
| Vol stability | 0.10 | 1 − |ln(vol_now / vol_long)| / ln 2 |

Rating: **High** ≥ 70, **Medium** ≥ 45, otherwise **Low**. High confidence means the ranges have been
reliable *and* the evidence points one way. It doesn't mean the direction is certain.

## 10.4 Recommendation rules

At the primary horizon *h* (default 63 trading days, about 3 months; the dashboard has buttons for 1M, 3M, 6M and 1Y):

```
excess  = E[return_h] − r_f·h/252
edge    = excess / σ_h                         (horizon Sharpe ratio)
score   = 0.5·clip(edge/0.5) + 0.3·clip((P(up) − 0.5)/0.2) + 0.2·clip(signal/60)      ∈ [−1, 1]

score ≥ 0.55 → Strong Buy   ≥ 0.25 → Buy   > −0.25 → Hold   > −0.55 → Reduce   else Sell
```

Guard-rails, applied in order:

1. **The simulation must agree.** Buy or Strong Buy needs P(up) > 50% *and* a positive excess return.
   Reduce or Sell needs the reverse. Otherwise the result is Hold, so a bullish chart alone can't produce a Buy.
2. **Low confidence** moves the call one step toward Hold.
3. **Strong Buy** also needs P(up) ≥ 58% and High confidence. **Sell** needs P(up) ≤ 42% and High confidence.
4. **High or Extreme baseline risk** caps the call at Buy.

Every step that changed the call is listed in `recommendation.notes`. The vol-target allocation is
`10% / horizon vol`, the portfolio weight that would add roughly 10% annualised volatility. It's a
sizing reference, not an instruction.

## 10.5 Where it shows up

| Place | What you see |
|---|---|
| `abg predict AAPL [-h 126] [--paths 20000] [--json]` | Full view: call, confidence, thesis, horizon table, scenarios, setup odds, calibration, confidence components |
| `abg analyze AAPL` | Compact prediction panel + horizon table |
| Dashboard → Analyze | **Prediction** card: call badge, confidence meter, thesis, fan chart (history + median / 25–75% / 5–95% paths), scenarios, horizon table, setup odds, "How this was calculated" |
| Dashboard → Portfolio | "Model view" pill under each holding's signal (from the live monitor) |
| Live monitor | `recommendation` signal when the model view changes (e.g. Hold → Buy); warning severity for Strong Buy / Sell or High-confidence changes |
| Claude insight | The prediction digest is sent to Claude, which is asked to agree or push back with evidence |
| REST | `GET /api/predict/{symbol}?horizon=63&paths=5000`; also `forecast` inside `/api/analyze` |

Settings: `ABG_FORECAST_PATHS` (5000), `ABG_FORECAST_HORIZON` (63), `ABG_FORECAST_EQUITY_PREMIUM` (0.05),
`ABG_FORECAST_SIGNAL_TILT` (0.25). The monitor uses 2,000 paths per symbol to stay light.

## 10.6 Validation

`tests/test_forecast.py` checks the following:

- reproducibility (same seed gives the same output)
- percentiles are ordered and the fan widens over time
- the simulated 1-year mean matches the analytic `exp(μ)−1`, and the simulated 1-month σ matches the variance term structure (±12%)
- the signal tilt shifts P(up) in the right direction
- barrier probabilities behave correctly for near and far levels and for long and short setups
- calibration gives about 90% / 50% coverage on a constant-vol process
- every guard-rail in the recommendation rules
- engine, API and monitor integration

Performance: about 90 ms for 5,000 × 252-day paths plus calibration (after the first-call imports).

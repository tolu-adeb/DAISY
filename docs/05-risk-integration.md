# 5. Risk-model integration

v3 is built so that a future risk model (statistical, ML or anything else) can be added
**without editing terminal code**, and receives a stable, documented input.

```
                     ┌───────────── build_features ──────────────┐
PriceHistory ─► indicators ─► build_feature_frame (per bar) ──┐   │
benchmark ────────────────────────────────────────────────────┤   ├─► FeatureVector (40 values, schema 1.0.0)
sentiment / options / fundamentals / signal (snapshot only) ──┘   │           │
                                                                              ▼
RiskContext (prices+indicators, returns, benchmark returns, feature_frame, position)
                                                                              │
                    run_models:  baseline │ your_model_1 │ your_model_2 …   (concurrent, isolated)
                                                                              ▼
                               report["risk"] = [RiskAssessment dicts]  (baseline first)
```

## 5.1 The feature schema (`risk/features.py`)

`FEATURE_SCHEMA_VERSION = "1.0.0"`. `abg schema` prints the table; `GET /api/schema` returns it as JSON.

| Group | Features | Historical? |
|---|---|---|
| returns | `ret_1d, ret_5d, ret_21d, ret_63d, ret_252d` | ✓ |
| volatility | `vol_20d, vol_60d, vol_252d` (annualised), `vol_ratio_20_252`, `parkinson_vol_20d`, `downside_vol_60d`, `atr_pct` | ✓ |
| tail | `var_95_1d`, `cvar_95_1d` (exact rolling historical, positive = loss), `skew_252d`, `kurt_252d`, `gap_freq_3pct` | ✓ |
| drawdown | `max_dd_252d` (peak and trough both inside the window), `current_dd` | ✓ |
| trend | `dist_sma50, dist_sma200, adx, rsi, macd_hist_atr, bb_pctb, bb_bw_pctile` | ✓ |
| liquidity | `rel_volume`, `log_dollar_volume_20d`, `amihud_illiq_20d` | ✓ |
| market | `beta_252d`, `corr_252d` (vs `ABG_BENCHMARK`) | ✓ |
| sentiment | `news_sentiment`, `news_count_24h` | snapshot |
| options | `atm_iv`, `iv_rv_spread`, `skew_25d`, `put_call_oi` (only from **market** chains, never model chains) | snapshot |
| fundamentals | `log_market_cap`, `pe` | snapshot |
| signal | `signal_score` | snapshot |

**Versioning rules**

- PATCH: documentation only.
- MINOR: features **added**. Existing models keep working because they select their own columns by name.
- MAJOR: a feature renamed, removed or re-scaled. Models declare `schema_major` and are refused
  (with a clear error in the report) if it doesn't match.

Missing values are `None` in JSON and `NaN` in arrays. `FeatureVector.coverage()` reports the fraction present.

**No look-ahead.** Every historical feature at bar *t* uses data ≤ *t* only. A test recomputes
the frame with the last 30 bars removed and asserts the overlapping rows are identical.

## 5.2 The `RiskModel` interface (`risk/interface.py`)

```python
class RiskModel(Protocol):
    name: str                 # unique id, shown in reports
    version: str
    schema_major: int         # FEATURE_SCHEMA major version the model was built for
    def assess(self, features: FeatureVector, ctx: RiskContext) -> RiskAssessment: ...
    # `assess` may also be `async def`
```

`RiskContext` fields:

| Field | Content |
|---|---|
| `symbol` | ticker |
| `prices` | full OHLCV + indicator DataFrame (includes warm-up) |
| `returns` | simple returns series |
| `benchmark_returns` | SPY returns or None |
| `feature_frame` | historical features with the same schema, useful for percentile ranks or sequence models |
| `position` | optional dict, e.g. `{"account_value": 50000, "risk_pct": 1, "entry": 100, "stop": 95, "shares": 200}` |
| `extras` | free-form |

`RiskAssessment` fields: `model, version, score (0–100), level, metrics{}, drivers[{factor, contribution, detail}], warnings[], schema_version`.
Levels are derived from the score if not given: < 25 Low, < 45 Moderate, < 65 Elevated, < 80 High, otherwise Extreme.

**Isolation:** each model runs concurrently with a 10 s timeout. Sync models run in a worker
thread. An exception, timeout or schema mismatch becomes `{"model": …, "error": …}` plus a
report warning, and never fails the analysis.

## 5.3 Registering a model (three ways, no terminal edits)

```python
# 1. in code
from abg.risk import register_risk_model
register_risk_model(MyModel())            # instance, class, or zero-arg factory
```

```bash
# 2. environment / .env  (module:attribute, comma-separated; CWD is importable)
ABG_RISK_MODELS=examples.custom_risk_model:VolTargetRiskModel abg analyze AAPL
```

```toml
# 3. packaging: entry point in *your* package's pyproject.toml
[project.entry-points."abg.risk_models"]
xgb = "abg_risk_ml.model:XGBRiskModel"
```

A working example lives in `examples/custom_risk_model.py`. It includes `VolTargetRiskModel` and a
`SklearnRiskModel` template that loads a trained model and feeds it `features.to_array(FEATURE_NAMES)`.

## 5.4 Training workflow

```bash
# 1) export a panel of features + forward labels (y_*), schema recorded alongside
abg features AAPL MSFT NVDA AMZN GOOGL META JPM XOM -p 10y --labels -o train.csv
#    → train.csv + train.schema.json
```

Labels, added by `add_forward_labels` (never use them as inputs):

| Label | Meaning |
|---|---|
| `y_fwd_ret_{h}d` | return over the next *h* bars |
| `y_fwd_vol_{h}d` | realised vol over the next *h* bars (annualised) |
| `y_fwd_max_loss_{h}d` | worst close-to-close loss from *t* within the next *h* bars |

Default horizons are 5 and 21. The last *h* rows are NaN by construction.

```python
# 2) train (sketch)
import pandas as pd
from abg.risk.features import TIMESERIES_FEATURES
df = pd.read_csv("train.csv", parse_dates=["date"]).dropna(subset=["y_fwd_max_loss_21d"])
X = df[TIMESERIES_FEATURES]
y = (df["y_fwd_max_loss_21d"] > 0.10).astype(int)      # event: >10% drawdown within a month
# use time-based splits (walk-forward), never random shuffles, to avoid leakage across time
```

(`abg.risk.features.TIMESERIES_FEATURES` lists the columns present in the export. Snapshot-only
features such as options and news aren't available historically, so train on the time-series subset,
or impute them.)

```python
# 3) wrap and register
class XGBRiskModel:
    name, version, schema_major = "xgb_drawdown", "1.0.0", 1
    def __init__(self): self.m = joblib.load("xgb.joblib")
    def assess(self, f, ctx):
        from abg.risk.features import TIMESERIES_FEATURES
        p = self.m.predict_proba(f.to_array(TIMESERIES_FEATURES).reshape(1, -1))[0, 1]
        return RiskAssessment(self.name, self.version, p * 100, level_for(p * 100), metrics={"p_event": p})
```

The baseline model is always reported first, so every new model is shown next to a
transparent benchmark.

## 5.5 The baseline model (`risk/baseline.py`)

Nine risk dimensions are each mapped to a 0–100 sub-score by piecewise-linear bands. The overall
score is the weighted mean over the dimensions that have data.

| Dimension | Feature | Weight | Band anchors (value → sub-score) |
|---|---|---|---|
| volatility | `vol_60d` | 0.22 | 10%→10, 20%→30, 35%→55, 60%→85, 100%→100 |
| tail loss | `cvar_95_1d` | 0.18 | 1%→10, 2%→30, 3.5%→55, 6%→85, 10%→100 |
| drawdown | `max_dd_252d` (70%) + `current_dd` (30%) | 0.14 | 5%→10, 15%→35, 30%→60, 50%→85, 75%→100 |
| vol regime | `vol_ratio_20_252` | 0.10 | 0.6→10, 0.9→30, 1.2→55, 1.8→85, 2.5→100 |
| market beta | \|`beta_252d`\| | 0.10 | 0.3→10, 0.8→30, 1.2→50, 1.8→80, 2.5→100 |
| liquidity | `log_dollar_volume_20d` | 0.10 | $0.3M→95, $5M→70, $50M→40, $1B→12, $10B→5 |
| gap risk | `gap_freq_3pct` | 0.08 | 0→5, 2%→35, 5%→65, 10%→90, 20%→100 |
| news sentiment | `news_sentiment` | 0.04 | −0.6→90, −0.2→65, 0→45, +0.3→25, +0.6→15 |
| vol risk premium | `iv_rv_spread` | 0.04 | −0.10→25, 0→40, +0.10→60, +0.25→85, +0.5→100 |

**Drivers** are ranked by contribution = (sub-score − 50) × weight / Σweights. A warning is added
if under 60% of the weight had data.

**Loss metrics** (last 252 returns): historical VaR/CVaR at 95 and 99%, parametric (normal)
VaR, 10-day VaR (√10 scaling), and **Cornish-Fisher VaR**, which adjusts the normal quantile for
skew and excess kurtosis. With `position` supplied, it also reports fixed-fractional position
sizing (`shares = floor(account × risk% / |entry − stop|)`) and the position's 1-day VaR in dollars.

## 5.6 Portfolio risk (`risk/portfolio.py`)

`portfolio_risk(closes, weights=None)` is used by `compare`. It returns annualised portfolio vol
(wᵀΣw), historical VaR/CVaR of the portfolio return series, the **diversification ratio**
(Σwᵢσᵢ / σₚ), per-asset **% risk contribution** (wᵢ(Σw)ᵢ/σₚ², which sums to 100%) and the
correlation matrix.

Next: [Interfaces →](06-interfaces.md)

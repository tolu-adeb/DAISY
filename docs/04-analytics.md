# 4. Analytics

Every function in `abg/analysis/` is pure: DataFrame/dict in, DataFrame/dict out, no I/O.
Values are NaN during warm-up and are never forward-filled or computed with look-ahead.

## 4.1 Indicators (`indicators.py`)

Notation: C/H/L/O/V = close/high/low/open/volume; TP = (H+L+C)/3; *Wilder(x, n)* =
EWM with α = 1/n (matches TradingView / TA-Lib after warm-up).

| Column(s) | Formula |
|---|---|
| `sma_{10,20,50,100,200}` | Rolling mean of C |
| `ema_{9,12,21,26,50}` | EWM(span=n, adjust=False) |
| `rsi_14` | 100 − 100/(1 + Wilder(gain)/Wilder(loss)); 100 when loss = 0; 50 when flat |
| `macd`, `macd_signal`, `macd_hist` | EMA12 − EMA26; EMA9 of MACD; difference |
| `bb_mid/upper/lower`, `bb_pctb`, `bb_bandwidth` | SMA20 ± 2σ (population σ); %B = (C − lower)/(upper − lower); bandwidth = (upper − lower)/mid |
| `atr_14`, `atr_pct` | Wilder(TrueRange); ATR / C × 100 |
| `plus_di`, `minus_di`, `adx` | Wilder-smoothed ±DM / ATR × 100; ADX = Wilder(DX) |
| `obv`, `obv_slope_20` | Σ sign(ΔC)·V; 20-bar OBV change ÷ (20 × avg volume) |
| `williams_r` | −100 × (HH14 − C)/(HH14 − LL14) |
| `cci_20` | (TP − SMA20(TP)) / (0.015 × mean abs deviation), vectorised with `sliding_window_view` |
| `stoch_k`, `stoch_d` | 100 × (C − LL14)/(HH14 − LL14); SMA3 |
| `mfi_14` | Money-flow index on TP × V |
| `vwap_20` | Daily bars: rolling 20-bar Σ(TP·V)/ΣV. Intraday: session VWAP reset each day |
| `rel_volume` | V / SMA20(V) |
| `high_20/low_20`, `high_252/low_252`, `roc_20`, `ret_1d` | Donchian ranges, rate of change, 1-bar return |

Tests check RSI against a textbook loop and CCI against a naive rolling apply, and check that all
oscillators stay in bounds.

## 4.2 Composite signal (`signals.composite_signal`)

Each component casts a **vote** in [−1, +1] with a **weight**. Score = 100 × Σ(w·v)/Σw, taken over
the components that have data.

| Component | Weight | Vote |
|---|---|---|
| trend | 0.25 | mean of sign(C − SMA50), sign(C − SMA200), sign(SMA50 − SMA200) |
| macd | 0.15 | 0.6·tanh(hist / 0.15·ATR) + 0.4·sign(Δhist) |
| rsi | 0.12 | ≥75 → −0.4 (overbought); 55–75 → +0.6; 45–55 → 0; 25–45 → −0.6; ≤25 → +0.4 (oversold bounce) |
| adx | 0.12 | (+DI − −DI)/(+DI + −DI) × min(ADX/25, 1) × 1.5, clipped |
| bollinger | 0.08 | %B > 1.05 → −0.5; %B < −0.05 → +0.5; else 0.8·(%B − 0.5) |
| obv | 0.10 | tanh(2 × OBV slope) |
| mfi | 0.07 | (MFI − 50)/30 inside 20–80; ≥80 → −0.4; ≤20 → +0.4 |
| oscillators | 0.06 | W%R < −80 and %K < 20 → +0.4; W%R > −20 and %K > 80 → −0.4; else (%K − 50)/60 |
| cci | 0.05 | CCI/200 inside ±200; beyond → −0.3·sign(CCI) (exhaustion) |

Labels: ≥ 50 Strong Bullish · ≥ 20 Bullish · > −20 Neutral · > −50 Bearish · else Strong Bearish.
The report includes every component's vote, weight and a plain-English reason.

## 4.3 Regime (`signals.market_regime`)

- **Trend**: uptrend if C > SMA50 > SMA200; downtrend if C < SMA50 < SMA200; otherwise sideways/transitioning.
- **Strength**: ADX ≥ 30 strong, ≥ 20 moderate, otherwise weak.
- **Volatility regime**: percentile of today's Bollinger bandwidth within the last 252 bars.
  ≤ 20th percentile is compressed, ≥ 80th is expanded.

## 4.4 Trade setups (`signals.classify_plays`)

Each setup is a list of conditions. **Confidence** is the fraction met. A setup qualifies
if confidence ≥ 0.6 **and** its first (defining) condition is met.

| Setup | Dir | Conditions (first = defining) |
|---|---|---|
| Momentum Breakout | long | C > prior 20-bar high · rel. vol ≥ 1.5 · RSI 55–78 · ADX ≥ 20 · MACD hist > 0 |
| Trend Pullback | long | C > SMA50 > SMA200 · RSI 38–55 · within 1 ATR of SMA20/50 · MACD hist rising |
| Oversold Mean Reversion | long | RSI ≤ 32 · %B < 0.05 · W%R ≤ −80 · C > SMA200 |
| Overextended / Fade | short | RSI ≥ 75 · %B > 1 · CCI ≥ 180 · MACD hist falling |
| Bearish Breakdown | short | C < prior 20-bar low · C < SMA50 < SMA200 · ADX ≥ 20 · MACD hist < 0 · rel. vol ≥ 1.3 |
| Volatility Squeeze | neutral | bandwidth in lowest 20% of 6 months · ADX < 20 · RSI 40–60 |

**Levels** for directional setups: entry = last close. The stop is the wider of 2 × ATR and the
10-bar swing low/high ± 0.25 ATR. Risk R = |entry − stop|. Targets are at 1.5R and 3R. When nothing
qualifies, the report shows "No Clear Setup".

## 4.5 Levels (`stats.support_resistance`)

- Classic floor pivots from the last bar: P = (H+L+C)/3, R1 = 2P − L, S1 = 2P − H, R2 = P + (H − L), S2 = P − (H − L).
- Swing highs/lows over 120 bars (local extrema, ±5 bars) are clustered within 1.5%. Clusters are ranked by
  touch count; the report shows up to 4 above and 4 below price.

## 4.6 Statistics (`stats.summary`)

Computed over the displayed period, with periods per year taken from the interval (252 for daily).

| Field | Definition |
|---|---|
| `total_return_pct`, `cagr_pct` | Cₙ/C₀ − 1; annualised if the period is ≥ 3 months |
| `ann_volatility_pct`, `vol_{20,60,252}d_pct` | std(log returns) × √ppy |
| `sharpe` | (mean simple return × ppy − rf) / ann. vol |
| `sortino` | same numerator / downside deviation |
| `calmar` | CAGR / |max drawdown| |
| `max_drawdown_pct` + peak/trough/recovery dates, `current_drawdown_pct` | from the running peak |
| `beta`, `correlation` | vs benchmark (SPY), 252 overlapping log returns |
| `relative_return_pct` | total return − benchmark return |
| `skew`, `excess_kurtosis`, `best/worst_day_pct`, `pct_up_days` | distribution shape |

## 4.7 Sentiment (`sentiment.py`)

Default model `lexicon-v2`:

1. **Event phrases** first ("beats estimates" +2.0, "cuts guidance" −2.2, "going concern" −2.5…).
2. **Word lexicon**: finance-specific positive and negative sets.
3. **Negation**: a negator in the preceding 3 tokens flips the sign and damps it (×−0.7).
4. **Intensifiers** ("sharply" ×1.5, "slightly" ×0.6).
5. Score = tanh(total / 2.5) ∈ (−1, 1).

If the vendor supplies its own per-ticker score (Alpha Vantage, Polygon insights), the final
article score is 0.5 × model + 0.5 × vendor.

Aggregate = recency-weighted mean with a half-life of 72 h. Labels: > 0.45 Very Positive, > 0.15
Positive, ≥ −0.15 Neutral, ≥ −0.45 Negative, otherwise Very Negative.

**Swapping in FinBERT or the fund's NLP layer:** implement `name` and `score(texts) -> list[float]`,
then call `set_sentiment_model(model)`. If the plug-in raises, the lexicon is used for that call.

## 4.8 Options (`options.py`)

**Black-Scholes-Merton** with continuous dividend yield *q*:

```
d1 = [ln(S/K) + (r − q + σ²/2)T] / (σ√T),   d2 = d1 − σ√T
C  = S e^(−qT) N(d1) − K e^(−rT) N(d2)
P  = K e^(−rT) N(−d2) − S e^(−qT) N(−d1)
```

| Greek | Formula (call; put analogues in code) | Unit |
|---|---|---|
| Δ | e^(−qT) N(d1) | per $1 |
| Γ | e^(−qT) φ(d1) / (Sσ√T) | per $1 |
| Vega | S e^(−qT) φ(d1) √T / 100 | per **1 vol point** |
| Θ | [−S e^(−qT) φ(d1) σ / (2√T) − rK e^(−rT) N(d2) + qS e^(−qT) N(d1)] / 365 | per **calendar day** |
| ρ | K T e^(−rT) N(d2) / 100 | per **1% rate** |
| P(ITM) | N(d2) (call), N(−d2) (put) | risk-neutral |

Tests check parity, the Hull textbook values (C = 4.76, P = 0.81) and every Greek against finite
differences.

**Implied vol** (`implied_vol`) is vectorised Newton-Raphson seeded with the Brenner-Subrahmanyam
approximation, with a bisection bracket [1e-4, 5]. The Newton step is replaced by bisection whenever it
leaves the bracket or vega is tiny, so it always converges when a solution exists. Prices outside no-arbitrage bounds
return NaN.

**Chain analytics** (`analyze_chain`):

- IV is recomputed from each contract's **mid**, because vendor IVs are often stale. It falls back to the vendor IV if the mid is unusable.
- Selected expiry: the requested one, else the first ≥ 5 days out.
- ATM IV is the mean call/put IV at the strike nearest spot. Expected move = S × IV_ATM × √T.
- 25Δ skew = IV(put nearest Δ −0.25) − IV(call nearest Δ +0.25).
- Put/call volume and OI ratios; **max pain** = the strike minimising total option-holder payout.
- Term structure: ATM IV for the first 8 expiries.
- Output: ±10 strikes around ATM with bid/ask/mid/theo/IV/Δ/Γ/Θ/vega/ρ/P(ITM)/OI/breakeven.

**Model chain fallback** (`theoretical_chain`): when no vendor returns a chain, the engine builds
next-Friday plus monthly (third-Friday) expiries and strikes around spot. It prices them with realised vol
(60 d, else 20 d) and a mild smile σ_K = σ(1 − 0.35m + 1.2m²), where m = ln(K/S). The result is flagged
`model_generated: true` in the report, CLI and dashboard.

## 4.9 AI insight (`insights.py`)

- A **digest** of about 40 key numbers (no raw price arrays) plus 6 headlines is sent to the Anthropic
  Messages API (`/v1/messages`, model `ABG_ANTHROPIC_MODEL`). This keeps the prompt to roughly 1–2k tokens.
- The system prompt requires JSON with these keys: summary, bull_case, bear_case, key_levels, risks_to_watch,
  stance, confidence. The parser tolerates fenced or chatty output.
- Responses are cached for 1 h, keyed by the SHA-256 of the digest.
- On any failure (no key, timeout, overload, bad JSON) the engine falls back to `rule_based_insight`, which is
  built deterministically from the signal components, sentiment, setups, levels and risk drivers.

Next: [Risk integration →](05-risk-integration.md)

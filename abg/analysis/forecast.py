"""Prediction: Monte Carlo simulation of future prices, plus a thesis, confidence rating and
recommendation.

What it is (and isn't)
----------------------
This module simulates thousands of plausible future price paths and summarises their
*distribution*: ranges, probabilities, tail losses, the odds of hitting a target before a
stop. It does **not** claim to know tomorrow's price. The central estimate uses deliberately
conservative drift (risk-free + beta x equity premium, plus a small, capped tilt from the
technical signal and news), because realised equity drift is tiny next to volatility and
historical drift estimates are extremely noisy. Most of the information is in the *width* and
*shape* of the distribution: volatility level, clustering, fat tails and skew. Those can be
estimated far more reliably, and the walk-forward calibration check below measures how well.

Models (ensemble, half the paths each)
--------------------------------------
1. **GBM-t**: log-returns with Student-t(ν=5) shocks scaled to unit variance and a volatility
   term structure that decays from today's EWMA vol (λ=0.94, RiskMetrics) towards the 3-year
   vol with a ~1-month half-life.
2. **Filtered historical simulation (FHS)**: historical returns are de-volatilised by their
   own EWMA vol, resampled in 10-day blocks (keeping short-range autocorrelation and the
   empirical skew/kurtosis), then re-scaled by the forecast vol term structure.

Both use the same drift. A large disagreement between their medians lowers confidence.

Outputs (all JSON-safe)
-----------------------
horizons      5/21/63/126/252-day: price & return percentiles, mean, P(up), P(±10%), VaR/CVaR
fan           daily 5/25/50/75/95 percentile price paths for the chart (next ~6 months)
scenarios     bear / base / bull at the primary horizon (quartile-conditional means)
barriers      for each directional setup: P(target before stop), P(stop first), expected days
calibration   walk-forward: coverage of past 90% intervals (should be ~90%) + sharpness
confidence    0-100 with its components; recommendation + thesis are added by `finalize`

Educational analytics only - not investment advice.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

HORIZONS = (5, 21, 63, 126, 252)
PCTS = (5, 10, 25, 50, 75, 90, 95)
REC_LEVELS = ["Sell", "Reduce", "Hold", "Buy", "Strong Buy"]


@dataclass
class ForecastConfig:
    paths: int = 5000
    primary_horizon: int = 63
    fan_days: int = 126
    equity_premium: float = 0.05        # long-run equity risk premium (annual)
    signal_tilt: float = 0.25           # max drift tilt = tilt x annual vol at |score| = 100
    sentiment_tilt: float = 0.02        # annual drift per unit of news sentiment (-1..1)
    student_df: float = 5.0
    ewma_lambda: float = 0.94
    vol_half_life: float = 22.0         # days for the vol forecast to revert halfway to long-run
    block: int = 10
    calibration_origins: int = 40
    min_bars: int = 150


# --------------------------------------------------------------------------- volatility model
def ewma_var(r: np.ndarray, lam: float) -> np.ndarray:
    """Causal EWMA variance: v[t] uses returns up to and including t."""
    v = np.empty_like(r)
    seed = np.nanvar(r[: min(len(r), 30)]) if len(r) else 0.0
    prev = seed if seed > 0 else 1e-4
    for i, x in enumerate(r):
        prev = lam * prev + (1 - lam) * x * x
        v[i] = prev
    return v


def vol_term_structure(v_short: float, v_long: float, horizon: int, half_life: float) -> np.ndarray:
    """Per-day variance for days 1..horizon, decaying from v_short to v_long (GARCH-like)."""
    k = math.log(2) / max(half_life, 1e-6)
    t = np.arange(1, horizon + 1)
    return v_long + (v_short - v_long) * np.exp(-k * t)


# --------------------------------------------------------------------------- simulation
def simulate(close: pd.Series, *, symbol: str = "", rf: float = 0.04, beta: float | None = None,
             signal_score: float | None = None, sentiment: float | None = None, plays: list | None = None,
             cfg: ForecastConfig | None = None, seed: int | None = None) -> dict:
    cfg = cfg or ForecastConfig()
    close = close.dropna()
    close = close[close > 0]
    if len(close) < cfg.min_bars:
        return {"available": False, "reason": f"need >= {cfg.min_bars} daily bars (have {len(close)})"}

    r = np.diff(np.log(close.to_numpy(dtype=float)))
    r = r[np.isfinite(r)]
    ev = ewma_var(r, cfg.ewma_lambda)
    long_win = r[-756:]
    v_long = float(np.var(long_win, ddof=1))
    v_short = float(ev[-1])
    H = max(max(HORIZONS), cfg.fan_days, cfg.primary_horizon)
    var_path = vol_term_structure(v_short, v_long, H, cfg.vol_half_life)
    ann_vol_long = math.sqrt(v_long * 252)
    ann_vol_now = math.sqrt(v_short * 252)

    # ---- drift (annual, arithmetic) with transparent components
    # beta floored at 0.3: even low-beta stocks have historically earned a positive premium
    b = 1.0 if beta is None or not np.isfinite(beta) else float(np.clip(beta, 0.3, 2.5))
    tilt_sig = cfg.signal_tilt * (signal_score or 0.0) / 100.0 * ann_vol_long
    tilt_sent = cfg.sentiment_tilt * float(np.clip(sentiment or 0.0, -1, 1))
    mu_annual = rf + b * cfg.equity_premium + tilt_sig + tilt_sent
    drift = {"risk_free": rf, "beta": b, "equity_premium": b * cfg.equity_premium, "signal_tilt": tilt_sig,
             "sentiment_tilt": tilt_sent, "total_annual": mu_annual}
    m = mu_annual / 252 - 0.5 * var_path                  # daily log drift (Ito correction per day)

    if seed is None:
        seed = zlib.crc32(f"{symbol}|{close.index[-1]}|{close.iloc[-1]:.4f}".encode())
    rng = np.random.default_rng(seed)
    n = max(200, int(cfg.paths))
    n1 = n // 2
    n2 = n - n1
    sd = np.sqrt(var_path)

    # model 1: GBM with Student-t shocks (unit variance)
    nu = cfg.student_df
    z = rng.standard_t(nu, size=(n1, H)) / math.sqrt(nu / (nu - 2))
    inc1 = m + sd * z

    # model 2: filtered historical simulation with block bootstrap
    resid = r / np.sqrt(np.maximum(np.concatenate([[ev[0]], ev[:-1]]), 1e-12))    # r_t / sigma_{t|t-1}
    resid = resid[np.isfinite(resid)]
    resid = (resid - resid.mean()) / resid.std(ddof=1)
    B = max(1, min(cfg.block, len(resid) // 4))
    nblocks = math.ceil(H / B)
    starts = rng.integers(0, len(resid) - B + 1, size=(n2, nblocks))
    idx = (starts[:, :, None] + np.arange(B)[None, None, :]).reshape(n2, -1)[:, :H]
    inc2 = m + sd * resid[idx]

    inc = np.vstack([inc1, inc2])
    logp = np.cumsum(inc, axis=1)
    s0 = float(close.iloc[-1])
    prices = s0 * np.exp(logp)                           # (n, H)
    rets = prices / s0 - 1

    # ---- horizon statistics
    hz = []
    for h in [h for h in HORIZONS if h <= H]:
        rh = rets[:, h - 1]
        q = np.percentile(rh, PCTS)
        k = max(1, int(0.05 * len(rh)))
        worst = np.sort(rh)[:k]
        med1 = float(np.median(rets[:n1, h - 1]))
        med2 = float(np.median(rets[n1:, h - 1]))
        hz.append({
            "days": h, "label": _hlabel(h),
            "return_pct": {f"p{p}": float(v * 100) for p, v in zip(PCTS, q)},
            "price": {f"p{p}": float(s0 * (1 + v)) for p, v in zip(PCTS, q)},
            "expected_return_pct": float(rh.mean() * 100), "std_pct": float(rh.std() * 100),
            "prob_up": float((rh > 0).mean()), "prob_up_10": float((rh > 0.10).mean()),
            "prob_down_10": float((rh < -0.10).mean()),
            "var_95_pct": float(-worst.max() * 100), "cvar_95_pct": float(-worst.mean() * 100),
            "model_medians_pct": {"gbm_t": med1 * 100, "fhs": med2 * 100},
        })

    # ---- fan chart (daily percentiles)
    fd = cfg.fan_days
    fq = np.percentile(prices[:, :fd], [5, 25, 50, 75, 95], axis=0)
    future_days = pd.bdate_range(pd.Timestamp(close.index[-1]) + pd.Timedelta(days=1), periods=fd)
    fan = {"time": [int(pd.Timestamp(d).tz_localize("UTC").timestamp()) for d in future_days],
           **{f"p{p}": [round(float(x), 4) for x in row] for p, row in zip((5, 25, 50, 75, 95), fq)}}

    # ---- scenarios at the primary horizon
    ph = min(cfg.primary_horizon, H)
    rp = rets[:, ph - 1]
    q25, q75 = np.percentile(rp, [25, 75])
    scen = []
    for name, mask, prob in (("Bear", rp <= q25, 0.25), ("Base", (rp > q25) & (rp < q75), 0.50), ("Bull", rp >= q75, 0.25)):
        seg = rp[mask]
        scen.append({"name": name, "probability": prob, "return_pct": float(seg.mean() * 100),
                     "price": float(s0 * (1 + seg.mean())),
                     "range_pct": [float(seg.min() * 100), float(seg.max() * 100)]})

    # ---- barrier probabilities for directional setups
    barriers = []
    for p in plays or []:
        lv = p.get("levels") or {}
        if p.get("direction") not in ("long", "short") or not lv.get("stop") or not lv.get("target_1"):
            continue
        barriers.append(_barrier(prices[:, :ph], p["name"], p["direction"], lv["stop"], lv["target_1"], lv.get("target_2")))

    prim = next(x for x in hz if x["days"] == max(d for d in HORIZONS if d <= ph)) if hz else {}
    calib = calibrate(r, ev, cfg)
    out = {
        "available": True, "model": "ensemble: GBM-t + filtered historical simulation", "paths": n, "seed": seed,
        "as_of_price": s0, "as_of": close.index[-1], "primary_horizon": ph,
        "volatility": {"now_annual_pct": ann_vol_now * 100, "long_run_annual_pct": ann_vol_long * 100,
                       "half_life_days": cfg.vol_half_life,
                       "horizon_annual_pct": math.sqrt(var_path[:ph].mean() * 252) * 100},
        "drift": drift, "horizons": hz, "fan": fan, "scenarios": scen, "barriers": barriers,
        "calibration": calib,
    }
    out["confidence"] = confidence(out, n_bars=len(close), signal_score=signal_score, primary=prim)
    return out


def _hlabel(h: int) -> str:
    return {5: "1 week", 21: "1 month", 63: "3 months", 126: "6 months", 252: "1 year"}.get(h, f"{h} days")


def _barrier(prices: np.ndarray, name: str, direction: str, stop: float, t1: float, t2: float | None) -> dict:
    n, H = prices.shape
    if direction == "long":
        hit_t = prices >= t1
        hit_s = prices <= stop
    else:
        hit_t = prices <= t1
        hit_s = prices >= stop
    ft = np.where(hit_t.any(1), hit_t.argmax(1), H + 1)
    fs = np.where(hit_s.any(1), hit_s.argmax(1), H + 1)
    target_first = (ft < fs) & (ft <= H)
    stop_first = (fs < ft) & (fs <= H)
    res = {"setup": name, "direction": direction, "stop": stop, "target_1": t1, "horizon_days": H,
           "prob_target_first": float(target_first.mean()), "prob_stop_first": float(stop_first.mean()),
           "prob_neither": float(1 - target_first.mean() - stop_first.mean()),
           "median_days_to_target": float(np.median(ft[target_first]) + 1) if target_first.any() else None}
    if t2:
        hit2 = (prices >= t2) if direction == "long" else (prices <= t2)
        f2 = np.where(hit2.any(1), hit2.argmax(1), H + 1)
        res["prob_target2_before_stop"] = float(((f2 < fs) & (f2 <= H)).mean())
        res["target_2"] = t2
    # expected R multiple ~ P(t1 first) x 1.5R - P(stop first) x 1R  (paths ending in between count as 0)
    res["expected_r_multiple"] = float(res["prob_target_first"] * 1.5 - res["prob_stop_first"] * 1.0)
    return res


# --------------------------------------------------------------------------- calibration
def calibrate(r: np.ndarray, ev: np.ndarray, cfg: ForecastConfig, h: int = 21) -> dict:
    """Walk-forward check of the volatility model: at past origins (every h days, no look-ahead)
    build the same h-day 90% interval and count how often the realised return landed inside."""
    n = len(r)
    origins = [t for t in range(n - h - 1, 252, -h)][: cfg.calibration_origins]
    if len(origins) < 8:
        return {"available": False, "reason": "not enough history for walk-forward calibration"}
    inside = within50 = 0
    widths, zs = [], []
    nu = cfg.student_df
    q90 = _t_quantile(0.95, nu) / math.sqrt(nu / (nu - 2))
    q50 = _t_quantile(0.75, nu) / math.sqrt(nu / (nu - 2))
    for t in origins:
        v_long = float(np.var(r[max(0, t - 755): t + 1], ddof=1))
        vp = vol_term_structure(float(ev[t]), v_long, h, cfg.vol_half_life)
        sig = math.sqrt(vp.sum())
        mu = -0.5 * vp.sum()
        real = float(r[t + 1: t + 1 + h].sum())
        z = (real - mu) / sig
        zs.append(z)
        inside += abs(z) <= q90
        within50 += abs(z) <= q50
        widths.append(2 * q90 * sig)
    k = len(origins)
    cov90, cov50 = inside / k, within50 / k
    err = abs(cov90 - 0.90) + 0.5 * abs(cov50 - 0.50)
    return {"available": True, "horizon_days": h, "origins": k, "coverage_90": cov90, "coverage_50": cov50,
            "avg_interval_width_pct": float(np.mean(widths) * 100), "z_std": float(np.std(zs)),
            "score": float(np.clip(1 - err / 0.30, 0, 1)),
            "verdict": ("well calibrated" if err < 0.08 else "reasonably calibrated" if err < 0.16
                        else "intervals too narrow" if cov90 < 0.90 else "intervals too wide")}


def _t_quantile(p: float, nu: float) -> float:
    try:
        from scipy.stats import t as _t
        return float(_t.ppf(p, nu))
    except Exception:  # pragma: no cover - fallback table for nu=5
        return {0.95: 2.015, 0.75: 0.727}.get(p, 1.645)


# --------------------------------------------------------------------------- confidence
def confidence(fc: dict, *, n_bars: int, signal_score: float | None, primary: dict) -> dict:
    comps = {}
    comps["data_history"] = (min(1.0, n_bars / 750), 0.15, f"{n_bars} daily bars")
    cal = fc.get("calibration") or {}
    if cal.get("available"):
        comps["calibration"] = (cal["score"], 0.30, f"past 90% bands covered {cal['coverage_90']:.0%} of outcomes "
                                                     f"({cal['origins']} walk-forward tests)")
    if primary:
        med = primary["model_medians_pct"]
        sd = max(primary["std_pct"], 1e-6)
        comps["model_agreement"] = (math.exp(-abs(med["gbm_t"] - med["fhs"]) / (0.25 * sd)), 0.15,
                                    f"model medians {med['gbm_t']:+.1f}% vs {med['fhs']:+.1f}%")
        edge = abs(primary["prob_up"] - 0.5)
        comps["directional_edge"] = (min(1.0, edge / 0.15), 0.15, f"P(up) {primary['prob_up']:.0%}")
    comps["signal_clarity"] = (min(1.0, abs(signal_score or 0) / 60), 0.15, f"signal score {signal_score or 0:+.0f}")
    v = fc["volatility"]
    ratio = v["now_annual_pct"] / max(v["long_run_annual_pct"], 1e-9)
    comps["vol_stability"] = (max(0.0, 1 - abs(math.log(ratio)) / math.log(2)), 0.10,
                              f"vol now {v['now_annual_pct']:.0f}% vs long-run {v['long_run_annual_pct']:.0f}%")
    tw = sum(w for _, w, _ in comps.values())
    score = 100 * sum(s * w for s, w, _ in comps.values()) / tw
    return {"score": round(score, 1), "rating": "High" if score >= 70 else "Medium" if score >= 45 else "Low",
            "components": {k: {"score": round(s, 3), "weight": w, "detail": d} for k, (s, w, d) in comps.items()}}


# --------------------------------------------------------------------------- recommendation + thesis
def finalize(report: dict, rf: float = 0.04) -> dict:
    """Add recommendation and thesis to report['forecast'] (needs signal, plays, risk, sentiment)."""
    fc = report.get("forecast") or {}
    if not fc.get("available"):
        return fc
    ph = fc["primary_horizon"]
    prim = next((h for h in fc["horizons"] if h["days"] == ph), fc["horizons"][-1])
    sig = (report.get("signal") or {}).get("score") or 0.0
    risk = next((x for x in report.get("risk") or [] if "error" not in x), {})
    risk_level = risk.get("level")
    exp_ret = prim["expected_return_pct"] / 100
    sd = max(prim["std_pct"] / 100, 1e-9)
    excess = exp_ret - rf * ph / 252
    edge = excess / sd
    pup = prim["prob_up"]
    # Normalisers: a horizon Sharpe of 0.5 (≈1.0 annualised over 3 months) or a 70/30 up/down split is "maxed out".
    e_c = float(np.clip(edge / 0.50, -1, 1))
    p_c = float(np.clip((pup - 0.5) / 0.20, -1, 1))
    s_c = float(np.clip(sig / 60, -1, 1))
    raw = 0.5 * e_c + 0.3 * p_c + 0.2 * s_c
    idx = 4 if raw >= 0.55 else 3 if raw >= 0.25 else 2 if raw > -0.25 else 1 if raw > -0.55 else 0
    notes = []
    # the simulation itself must agree with the direction, not just the technicals
    if idx > 2 and not (pup > 0.5 and excess > 0):
        idx = 2
        notes.append("technicals are positive but the simulated odds are not better than a coin flip, so Hold")
    if idx < 2 and not (pup < 0.5 and excess < 0):
        idx = 2
        notes.append("technicals are negative but the simulated odds do not favour a decline, so Hold")
    conf = fc["confidence"]
    if conf["rating"] == "Low" and idx != 2:
        idx += -1 if idx > 2 else 1
        notes.append("moved one step toward Hold because model confidence is low")
    if idx == 4 and (pup < 0.58 or conf["rating"] != "High"):
        idx = 3
        notes.append("Strong Buy needs P(up) >= 58% and high confidence; shown as Buy")
    if idx == 0 and (pup > 0.42 or conf["rating"] != "High"):
        idx = 1
        notes.append("Sell needs P(up) <= 42% and high confidence; shown as Reduce")
    if risk_level in ("High", "Extreme") and idx == 4:
        idx = 3
        notes.append(f"capped at Buy because baseline risk is {risk_level}")
    action = REC_LEVELS[idx]
    vol = fc["volatility"]["horizon_annual_pct"] / 100
    alloc = float(np.clip(0.10 / max(vol, 1e-6), 0, 1))
    levels = report.get("levels") or {}
    sup = (levels.get("support") or [None])[0]
    top_long = next((p for p in report.get("plays") or [] if p.get("direction") == "long" and p.get("levels")), None)
    stop = (top_long or {}).get("levels", {}).get("stop")
    zone_lo = max(x for x in (sup, stop * 1.01 if stop else None, fc["as_of_price"] * 0.9) if x) if (sup or stop) else None
    rec = {
        "action": action, "score": round(float(raw), 3), "horizon_days": ph, "horizon_label": prim["label"],
        "expected_return_pct": prim["expected_return_pct"], "excess_return_pct": excess * 100,
        "prob_up": pup, "risk_adjusted_edge": float(edge), "notes": notes,
        "risk_level": risk_level, "vol_target_allocation_pct": alloc * 100,
        "levels": {"entry_zone": [zone_lo, fc["as_of_price"]] if zone_lo and zone_lo < fc["as_of_price"]
                   and action in ("Buy", "Strong Buy") else None,
                   "stop": stop if action in ("Buy", "Strong Buy") else None,
                   "upside_p75": prim["price"]["p75"], "upside_p90": prim["price"]["p90"],
                   "downside_p10": prim["price"]["p10"], "downside_p25": prim["price"]["p25"]},
        "components": {"risk_adjusted_edge": round(e_c, 3), "probability_edge": round(p_c, 3),
                       "technical_signal": round(s_c, 3), "weights": {"risk_adjusted_edge": 0.5, "probability_edge": 0.3,
                                                                       "technical_signal": 0.2}},
    }
    fc["recommendation"] = rec
    fc["thesis"] = build_thesis(report, fc, rec)
    return fc


def build_thesis(report: dict, fc: dict, rec: dict) -> dict:
    sym = report.get("symbol", "")
    prim = next(h for h in fc["horizons"] if h["days"] == rec["horizon_days"])
    reg = report.get("regime") or {}
    sig = report.get("signal") or {}
    comps = sig.get("components") or {}
    names = {"trend": "Trend", "macd": "MACD", "rsi": "RSI", "adx": "ADX/DI", "bollinger": "Bollinger",
             "obv": "Volume (OBV)", "mfi": "Money flow", "oscillators": "Oscillators", "cci": "CCI"}
    ranked = sorted(comps.items(), key=lambda kv: abs(kv[1]["vote"] * kv[1]["weight"]), reverse=True)
    bull = [f"{names.get(k, k)}: {c['reason']}" for k, c in ranked if c["vote"] > 0.25]
    bear = [f"{names.get(k, k)}: {c['reason']}" for k, c in ranked if c["vote"] < -0.25]
    sent = report.get("sentiment") or {}
    if sent.get("score") is not None and sent.get("articles", 0) >= 3:
        (bull if sent["score"] > 0.15 else bear if sent["score"] < -0.15 else []).append(
            f"news sentiment {sent['label'].lower()} ({sent['score']:+.2f})")
    v = fc["volatility"]
    if v["now_annual_pct"] > 1.25 * v["long_run_annual_pct"]:
        bear.append(f"volatility elevated ({v['now_annual_pct']:.0f}% vs {v['long_run_annual_pct']:.0f}% norm), so ranges are wide")
    elif v["now_annual_pct"] < 0.8 * v["long_run_annual_pct"]:
        bull.append(f"volatility subdued ({v['now_annual_pct']:.0f}% vs {v['long_run_annual_pct']:.0f}% norm)")
    fund = report.get("fundamentals") or {}
    if fund.get("forward_pe") and fund.get("pe") and fund["forward_pe"] < fund["pe"] * 0.9:
        bull.append(f"earnings expected to grow (forward P/E {fund['forward_pe']:.1f} vs trailing {fund['pe']:.1f})")
    risk = next((x for x in report.get("risk") or [] if "error" not in x), {})
    for d in (risk.get("drivers") or [])[:2]:
        if d.get("sub_score", 0) >= 60:
            bear.append(f"risk driver: {d['factor'].replace('_', ' ')} ({d.get('detail', '')})")
    b = (fc.get("barriers") or [None])[0]
    setup_line = None
    if b:
        setup_line = (f"{b['setup']} setup: {b['prob_target_first']:.0%} of simulated paths reach target "
                      f"{b['target_1']:,.2f} before stop {b['stop']:,.2f} within {b['horizon_days']} days "
                      f"(stop first {b['prob_stop_first']:.0%}).")
    lo, hi = prim["price"]["p10"], prim["price"]["p90"]
    headline = (f"{rec['action']} ({fc['confidence']['rating'].lower()} confidence): over {prim['label']} the model "
                f"centres on {prim['return_pct']['p50']:+.1f}% (mean {prim['expected_return_pct']:+.1f}%) with an "
                f"80% range of {lo:,.2f}-{hi:,.2f} and a {prim['prob_up']:.0%} chance of finishing higher.")
    context = (f"{sym} is in {'an' if str(reg.get('trend', '')).startswith('u') else 'a'} {reg.get('trend', 'n/a')} "
               f"({reg.get('trend_strength') or 'unclear'} strength) with a {sig.get('label', 'n/a').lower()} composite "
               f"signal ({sig.get('score', 0):+.0f}/100).")
    invalidation = []
    rl = rec["levels"]
    if rec["action"] in ("Buy", "Strong Buy"):
        stop = rl.get("stop") or rl.get("downside_p10")
        invalidation.append(f"a close below {stop:,.2f} would invalidate the bullish case")
    elif rec["action"] in ("Sell", "Reduce"):
        invalidation.append(f"a move above {rl['upside_p75']:,.2f} would argue against the bearish view")
    else:
        invalidation.append(f"a break outside {rl['downside_p25']:,.2f}-{rl['upside_p75']:,.2f} would signal a new direction")
    invalidation.append(f"a volatility spike would widen the downside (1-in-20 loss over {prim['label']}: "
                        f"{prim['var_95_pct']:.1f}%)")
    return {"headline": headline, "context": context, "bull_points": bull[:5] or ["no strong bullish evidence"],
            "bear_points": bear[:5] or ["no strong bearish evidence"], "setup": setup_line,
            "invalidation": invalidation,
            "method": ("Drift = risk-free + beta x equity premium + small signal/news tilt; volatility from EWMA decaying "
                       "to the 3-year level; fat tails from Student-t shocks and resampled historical shocks."),
            "disclaimer": "Simulated probabilities from historical behaviour, not a promise of future returns. "
                          "Educational analysis, not investment advice."}

"""Baseline risk model - transparent, rules-based, no training required.

It maps each risk dimension to a 0-100 sub-score with piecewise-linear bands, then takes
a weighted average over whatever dimensions have data.  It exists to (a) give a useful
risk read today and (b) serve as the benchmark any future ML model must beat.

Dimension           feature(s)                    weight
------------------  ----------------------------  ------
volatility          vol_60d                       0.22
tail loss           cvar_95_1d                    0.18
drawdown            max_dd_252d / current_dd      0.14
vol regime shift    vol_ratio_20_252              0.10
market sensitivity  |beta_252d|                   0.10
liquidity           log_dollar_volume_20d         0.10
gap risk            gap_freq_3pct                 0.08
news sentiment      news_sentiment                0.04
vol risk premium    iv_rv_spread                  0.04
"""
from __future__ import annotations

import math

import numpy as np

from .features import FEATURE_SCHEMA_VERSION, FeatureVector
from .interface import RiskAssessment, RiskContext, level_for

Z = {0.95: 1.6448536, 0.99: 2.3263479}


def _interp(x: float, pts: list[tuple[float, float]]) -> float:
    xs, ys = zip(*pts)
    return float(np.interp(x, xs, ys))


def position_size(account_value: float, risk_pct: float, entry: float, stop: float) -> dict:
    """Fixed-fractional sizing: risk ``risk_pct``% of the account between entry and stop."""
    per_share = abs(entry - stop)
    if per_share <= 0 or entry <= 0:
        return {}
    budget = account_value * risk_pct / 100
    shares = math.floor(budget / per_share)
    return {"shares": shares, "position_value": round(shares * entry, 2), "risk_amount": round(shares * per_share, 2),
            "position_pct_of_account": round(shares * entry / account_value * 100, 2) if account_value else None}


class BaselineRiskModel:
    name = "baseline"
    version = "1.0.0"
    schema_major = int(FEATURE_SCHEMA_VERSION.split(".")[0])

    BANDS = {
        "volatility":    ("vol_60d",            0.22, [(0.10, 10), (0.20, 30), (0.35, 55), (0.60, 85), (1.0, 100)]),
        "tail_loss":     ("cvar_95_1d",         0.18, [(0.01, 10), (0.02, 30), (0.035, 55), (0.06, 85), (0.10, 100)]),
        "drawdown":      ("max_dd_252d",        0.14, [(0.05, 10), (0.15, 35), (0.30, 60), (0.50, 85), (0.75, 100)]),
        "vol_regime":    ("vol_ratio_20_252",   0.10, [(0.6, 10), (0.9, 30), (1.2, 55), (1.8, 85), (2.5, 100)]),
        "market_beta":   ("beta_252d",          0.10, [(0.3, 10), (0.8, 30), (1.2, 50), (1.8, 80), (2.5, 100)]),
        "liquidity":     ("log_dollar_volume_20d", 0.10, [(5.5, 95), (6.7, 70), (7.7, 40), (9.0, 12), (10.0, 5)]),
        "gap_risk":      ("gap_freq_3pct",      0.08, [(0.0, 5), (0.02, 35), (0.05, 65), (0.10, 90), (0.2, 100)]),
        "news_sentiment": ("news_sentiment",    0.04, [(-0.6, 90), (-0.2, 65), (0.0, 45), (0.3, 25), (0.6, 15)]),
        "vol_premium":   ("iv_rv_spread",       0.04, [(-0.10, 25), (0.0, 40), (0.10, 60), (0.25, 85), (0.5, 100)]),
    }

    def assess(self, features: FeatureVector, ctx: RiskContext) -> RiskAssessment:
        subs: dict[str, tuple[float, float, str]] = {}
        for dim, (feat, w, pts) in self.BANDS.items():
            x = features.get(feat)
            if x is None or (isinstance(x, float) and not math.isfinite(x)):
                continue
            if dim == "market_beta":
                x = abs(x)
            subs[dim] = (_interp(x, pts), w, f"{feat}={x:.4g}")
        cur = features.get("current_dd")
        if "drawdown" in subs and cur is not None:              # blend current drawdown in
            s, w, d = subs["drawdown"]
            subs["drawdown"] = (0.7 * s + 0.3 * _interp(cur, self.BANDS["drawdown"][2]), w, d + f", current_dd={cur:.3f}")

        warnings = []
        if not subs:
            return RiskAssessment(self.name, self.version, 50.0, "Unknown", warnings=["insufficient data"])
        tw = sum(w for _, w, _ in subs.values())
        score = sum(s * w for s, w, _ in subs.values()) / tw
        if tw < 0.6:
            warnings.append(f"only {tw:.0%} of risk dimensions had data")
        drivers = sorted(({"factor": k, "sub_score": round(s, 1), "weight": w,
                           "contribution": round((s - 50) * w / tw, 2), "detail": d} for k, (s, w, d) in subs.items()),
                         key=lambda x: x["contribution"], reverse=True)

        # ---- loss metrics from the return distribution ----------------------
        r = ctx.returns.dropna().iloc[-252:]
        metrics: dict = {"observations": int(len(r))}
        if len(r) >= 30:
            mu, sd = float(r.mean()), float(r.std(ddof=1))
            for a in (0.95, 0.99):
                k = max(1, int(np.ceil((1 - a) * len(r))))
                worst = np.sort(r.to_numpy())[:k]
                tag = int(a * 100)
                metrics[f"var_{tag}_1d_pct"] = round(-worst.max() * 100, 3)
                metrics[f"cvar_{tag}_1d_pct"] = round(-worst.mean() * 100, 3)
                metrics[f"param_var_{tag}_1d_pct"] = round((Z[a] * sd - mu) * 100, 3)
            metrics["var_95_10d_pct"] = round(metrics["var_95_1d_pct"] * math.sqrt(10), 3)
            # Cornish-Fisher VaR adjusts the normal quantile for skew & fat tails
            s, k = float(r.skew()), float(r.kurt())
            z = -Z[0.95]
            zcf = z + (z**2 - 1) * s / 6 + (z**3 - 3 * z) * k / 24 - (2 * z**3 - 5 * z) * s**2 / 36
            metrics["cornish_fisher_var_95_1d_pct"] = round(-(mu + zcf * sd) * 100, 3)
        for key in ("vol_60d", "max_dd_252d", "current_dd", "beta_252d", "gap_freq_3pct"):
            if features.get(key) is not None:
                metrics[key] = round(features.get(key), 4)

        pos = ctx.position or {}
        if pos.get("account_value") and pos.get("entry") and pos.get("stop"):
            metrics["position_sizing"] = position_size(pos["account_value"], pos.get("risk_pct", 1.0),
                                                       pos["entry"], pos["stop"])
        if pos.get("shares") and "var_95_1d_pct" in metrics and len(ctx.prices):
            val = pos["shares"] * float(ctx.prices["close"].iloc[-1])
            metrics["position_var_95_1d"] = round(val * metrics["var_95_1d_pct"] / 100, 2)

        return RiskAssessment(self.name, self.version, round(score, 1), level_for(score), metrics, drivers, warnings)

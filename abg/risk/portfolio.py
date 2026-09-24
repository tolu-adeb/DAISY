"""Multi-asset risk: correlation, portfolio volatility / VaR, risk contributions."""
from __future__ import annotations

import numpy as np
import pandas as pd


def portfolio_risk(closes: pd.DataFrame, weights: dict[str, float] | None = None, ppy: float = 252,
                   window: int = 252) -> dict:
    """``closes``: one column per symbol.  Equal weights if none given."""
    rets = closes.pct_change(fill_method=None).dropna(how="all").iloc[-window:].dropna(axis=1, how="all").dropna()
    if rets.shape[0] < 30 or rets.shape[1] < 1:
        return {"available": False, "reason": "need >= 30 overlapping observations"}
    syms = list(rets.columns)
    w = np.array([(weights or {}).get(s, 1.0) for s in syms], dtype=float)
    w = w / w.sum()
    cov = rets.cov().to_numpy() * ppy
    port_var = float(w @ cov @ w)
    port_vol = np.sqrt(port_var)
    mcr = cov @ w / port_vol                      # marginal contribution to risk
    pcr = w * mcr / port_vol                      # % contribution (sums to 1)
    pr = rets.to_numpy() @ w
    k = max(1, int(np.ceil(0.05 * len(pr))))
    worst = np.sort(pr)[:k]
    indiv_vol = np.sqrt(np.diag(cov))
    return {
        "available": True, "symbols": syms, "weights": dict(zip(syms, w.round(4))), "observations": int(len(rets)),
        "ann_volatility_pct": port_vol * 100,
        "var_95_1d_pct": float(-worst.max() * 100), "cvar_95_1d_pct": float(-worst.mean() * 100),
        "diversification_ratio": float((w @ indiv_vol) / port_vol),
        "risk_contribution_pct": dict(zip(syms, (pcr * 100).round(2))),
        "correlation": rets.corr().round(3).to_dict(),
    }

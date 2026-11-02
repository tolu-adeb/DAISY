"""Factor screen: momentum, value, quality, growth (revisions proxy) and low volatility, ranked across the universe.

Each factor is a cross-sectional z-score (winsorised at +/-3); the composite is the weighted mean of whatever factors a
name has data for, so one missing field drops that factor instead of the name.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

# the large caps the screen covers when ABG_PITCH_UNIVERSE is empty (your watchlist is added on top)
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "QCOM", "TXN", "INTU",
    "JPM", "BAC", "GS", "MS", "V", "MA", "BRK-B", "UNH", "LLY", "JNJ", "MRK", "ABBV", "TMO", "PFE",
    "XOM", "CVX", "COP", "HD", "LOW", "COST", "WMT", "PG", "KO", "PEP", "MCD", "NKE", "SBUX",
    "CAT", "DE", "HON", "GE", "UNP", "LMT", "NFLX", "DIS", "TSLA",
]
WEIGHTS = {"momentum": 0.30, "value": 0.25, "quality": 0.25, "growth": 0.15, "low_vol": 0.05}
LABELS = {"momentum": "12-1 month momentum", "value": "earnings yield", "quality": "profit margin",
          "growth": "expected EPS growth (forward vs trailing)", "low_vol": "low 3-month volatility"}


def universe(settings, extra: list[str] | None = None) -> list[str]:
    raw = getattr(settings, "pitch_universe", "") or ""
    base = [s.strip().upper() for s in raw.replace("\n", ",").split(",") if s.strip()] or list(DEFAULT_UNIVERSE)
    out, seen = [], set()
    for s in base + [x.upper() for x in (extra or [])]:
        if s and s not in seen and not s.startswith("^") and "=" not in s and "/" not in s:
            seen.add(s)
            out.append(s)
    return out


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def raw_factors(close: pd.Series | None, fund: dict | None) -> dict:
    """The raw factor values for one name (None where data is missing)."""
    out = dict.fromkeys(WEIGHTS)
    fund = fund or {}
    if close is not None and len(close) >= 63:
        c = close.dropna().astype(float)
        if len(c) >= 253:
            out["momentum"] = c.iloc[-22] / c.iloc[-253] - 1
        elif len(c) >= 127:
            out["momentum"] = c.iloc[-22] / c.iloc[0] - 1
        vol = c.pct_change().iloc[-63:].std() * math.sqrt(252)
        out["low_vol"] = -vol if vol == vol else None
        out["ret_3m"] = c.iloc[-1] / c.iloc[-64] - 1 if len(c) >= 64 else None
        out["vol_3m"] = vol if vol == vol else None
    pe, fpe = _f(fund.get("pe")), _f(fund.get("forward_pe"))
    ey = [1 / x for x in (fpe, pe) if x and x > 0]
    out["value"] = ey[0] if ey else None
    pm = _f(fund.get("profit_margin"))
    om = _f((fund.get("extra") or {}).get("operating_margin"))
    out["quality"] = pm if pm is not None else om
    if pe and fpe and pe > 0 and fpe > 0:
        out["growth"] = max(-0.5, min(1.0, pe / fpe - 1))
    else:
        eg = _f((fund.get("extra") or {}).get("eps_growth_ttm_yoy_pct"))
        out["growth"] = max(-0.5, min(1.0, eg / 100)) if eg is not None else None
    return out


def rank(rows: dict[str, dict]) -> list[dict]:
    """``rows`` = {symbol: raw_factors}.  Returns rows sorted best first with z-scores, composite and rank."""
    syms = list(rows)
    if not syms:
        return []
    z: dict[str, dict] = {s: {} for s in syms}
    for k in WEIGHTS:
        vals = {s: rows[s].get(k) for s in syms if rows[s].get(k) is not None}
        if len(vals) < 3:
            continue
        arr = np.array(list(vals.values()), dtype=float)
        mu, sd = arr.mean(), arr.std()
        if not sd:
            continue
        for s, v in vals.items():
            z[s][k] = float(max(-3.0, min(3.0, (v - mu) / sd)))
    out = []
    for s in syms:
        have = {k: v for k, v in z[s].items()}
        w = sum(WEIGHTS[k] for k in have)
        comp = sum(WEIGHTS[k] * v for k, v in have.items()) / w if w else None
        out.append({"symbol": s, "raw": rows[s], "z": have, "composite": comp, "coverage": round(w, 2)})
    out.sort(key=lambda r: (r["composite"] is None, -(r["composite"] or 0)))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out

"""Return / risk statistics on a price series."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def periods_per_year(interval: str) -> float:
    return {"1d": 252, "1wk": 52, "1mo": 12, "1h": 252 * 6.5, "30m": 252 * 13, "15m": 252 * 26,
            "5m": 252 * 78, "1m": 252 * 390}.get(interval, 252)


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1)).dropna()


def max_drawdown(close: pd.Series) -> dict:
    peak = close.cummax()
    dd = close / peak - 1
    if dd.empty:
        return {"max_drawdown_pct": None}
    trough = dd.idxmin()
    peak_date = close.loc[:trough].idxmax()
    rec = close.loc[trough:]
    recovered = rec[rec >= close.loc[peak_date]]
    return {"max_drawdown_pct": float(dd.min() * 100), "peak_date": peak_date, "trough_date": trough,
            "recovery_date": recovered.index[0] if len(recovered) else None,
            "current_drawdown_pct": float(dd.iloc[-1] * 100)}


def realized_vol(close: pd.Series, window: int, ppy: float = TRADING_DAYS) -> float | None:
    r = log_returns(close).iloc[-window:]
    return float(r.std(ddof=1) * np.sqrt(ppy)) if len(r) >= max(5, window // 2) else None


def beta_corr(asset: pd.Series, bench: pd.Series, window: int = 252) -> dict:
    a = log_returns(asset)
    b = log_returns(bench)
    j = pd.concat([a, b], axis=1, join="inner").dropna().iloc[-window:]
    if len(j) < 30:
        return {"beta": None, "correlation": None, "n": len(j)}
    cov = np.cov(j.iloc[:, 0], j.iloc[:, 1], ddof=1)
    return {"beta": float(cov[0, 1] / cov[1, 1]), "correlation": float(j.corr().iloc[0, 1]), "n": len(j)}


def summary(close: pd.Series, interval: str = "1d", rf: float = 0.04, bench: pd.Series | None = None) -> dict:
    ppy = periods_per_year(interval)
    r = log_returns(close)
    simple = close.pct_change(fill_method=None).dropna()
    out: dict = {"observations": int(len(close))}
    if len(r) < 2:
        return out
    years = max(len(r) / ppy, 1e-9)
    total = close.iloc[-1] / close.iloc[0] - 1
    ann_vol = float(r.std(ddof=1) * np.sqrt(ppy))
    ann_ret = float((1 + total) ** (1 / years) - 1) if years >= 0.25 else None
    downside = simple[simple < 0]
    dd_vol = float(downside.std(ddof=1) * np.sqrt(ppy)) if len(downside) > 2 else None
    mean_excess = float(simple.mean() * ppy - rf)
    out.update({
        "total_return_pct": float(total * 100),
        "cagr_pct": None if ann_ret is None else ann_ret * 100,
        "ann_volatility_pct": ann_vol * 100,
        "vol_20d_pct": _pct(realized_vol(close, 20, ppy)),
        "vol_60d_pct": _pct(realized_vol(close, 60, ppy)),
        "vol_252d_pct": _pct(realized_vol(close, 252, ppy)),
        "sharpe": mean_excess / ann_vol if ann_vol else None,
        "sortino": mean_excess / dd_vol if dd_vol else None,
        "skew": float(r.skew()), "excess_kurtosis": float(r.kurt()),
        "best_day_pct": float(simple.max() * 100), "worst_day_pct": float(simple.min() * 100),
        "pct_up_days": float((simple > 0).mean() * 100),
        **max_drawdown(close),
    })
    if out.get("cagr_pct") is not None and out.get("max_drawdown_pct"):
        out["calmar"] = out["cagr_pct"] / abs(out["max_drawdown_pct"])
    if bench is not None and len(bench) > 30:
        out.update(beta_corr(close, bench))
        bt = bench.loc[bench.index >= close.index[0]]
        if len(bt) > 1:
            out["benchmark_return_pct"] = float((bt.iloc[-1] / bt.iloc[0] - 1) * 100)
            out["relative_return_pct"] = out["total_return_pct"] - out["benchmark_return_pct"]
    return out


def _pct(x: float | None) -> float | None:
    return None if x is None else x * 100


def support_resistance(df: pd.DataFrame, lookback: int = 120, order: int = 5, max_levels: int = 4) -> dict:
    """Classic floor pivots from the last bar + clustered swing highs/lows as S/R levels."""
    last = df.iloc[-1]
    h, l, c = float(last["high"]), float(last["low"]), float(last["close"])
    p = (h + l + c) / 3
    pivots = {"pivot": p, "r1": 2 * p - l, "s1": 2 * p - h, "r2": p + (h - l), "s2": p - (h - l)}
    w = df.iloc[-lookback:]
    hi, lo = w["high"].to_numpy(), w["low"].to_numpy()
    swings_hi, swings_lo = [], []
    for i in range(order, len(w) - order):
        if hi[i] == hi[i - order:i + order + 1].max():
            swings_hi.append(hi[i])
        if lo[i] == lo[i - order:i + order + 1].min():
            swings_lo.append(lo[i])

    def cluster(levels: list[float], tol: float = 0.015) -> list[float]:
        out: list[list[float]] = []
        for x in sorted(levels):
            if out and abs(x / np.mean(out[-1]) - 1) < tol:
                out[-1].append(x)
            else:
                out.append([x])
        # strongest (most touches) first
        return [float(np.mean(g)) for g in sorted(out, key=len, reverse=True)]

    res = [x for x in cluster(swings_hi) if x > c][:max_levels]
    sup = [x for x in cluster(swings_lo) if x < c][:max_levels]
    return {"pivots": pivots, "resistance": sorted(res), "support": sorted(sup, reverse=True)}

"""Performance statistics: equity-curve metrics and per-trade R statistics."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def equity_stats(equity: pd.Series, periods_per_year: int = 252) -> dict:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    rets = equity.pct_change().dropna()
    years = max(len(rets) / periods_per_year, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0 if equity.iloc[-1] > 0 else -1.0
    sd = rets.std(ddof=1)
    sharpe = rets.mean() / sd * math.sqrt(periods_per_year) if sd > 0 else float("nan")
    dsd = rets[rets < 0].std(ddof=1)
    sortino = rets.mean() / dsd * math.sqrt(periods_per_year) if dsd and dsd > 0 else float("nan")
    peak = equity.cummax()
    dd = equity / peak - 1.0
    max_dd = dd.min()
    # longest drawdown in periods
    under = dd < 0
    longest, cur = 0, 0
    for u in under:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    return {
        "total_return": float(total),
        "cagr": float(cagr),
        "ann_vol": float(sd * math.sqrt(periods_per_year)) if sd == sd else float("nan"),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": float(max_dd),
        "longest_dd_periods": int(longest),
        "calmar": float(cagr / abs(max_dd)) if max_dd < 0 else float("nan"),
    }


def bootstrap_ci(x: np.ndarray, level: float = 0.90, n: int = 5000, seed: int = 7) -> tuple[float, float]:
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def r_stats(r: pd.Series | np.ndarray, win_threshold: float = 0.0) -> dict:
    """Statistics of per-trade results expressed in R (net of costs)."""
    r = np.asarray(pd.Series(r).dropna(), dtype=float)
    n = len(r)
    if n == 0:
        return {"trades": 0}
    wins, losses = r[r > win_threshold], r[r <= win_threshold]
    mean = r.mean()
    sd = r.std(ddof=1) if n > 1 else float("nan")
    lo, hi = bootstrap_ci(r)
    gross_win, gross_loss = wins.sum(), -losses.sum()
    return {
        "trades": int(n),
        "net_R": float(r.sum()),
        "avg_R": float(mean),
        "t_stat": float(mean / (sd / math.sqrt(n))) if n > 1 and sd > 0 else float("nan"),
        "ci90_low": lo,
        "ci90_high": hi,
        "win_rate": float(len(wins) / n),
        "avg_win_R": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_R": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_dd_R": float(_max_dd_cum(r)),
    }


def _max_dd_cum(r: np.ndarray) -> float:
    c = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], c]))[1:]
    return float((c - peak).min()) if len(c) else 0.0


def group_r_stats(trades: pd.DataFrame, by: str, r_col: str = "net_R") -> pd.DataFrame:
    rows = []
    for k, g in trades.groupby(by, sort=True):
        s = r_stats(g[r_col])
        s[by] = k
        rows.append(s)
    if not rows:
        return pd.DataFrame()
    cols = [by, "trades", "net_R", "avg_R", "t_stat", "ci90_low", "ci90_high", "win_rate", "profit_factor", "max_dd_R"]
    return pd.DataFrame(rows)[cols]

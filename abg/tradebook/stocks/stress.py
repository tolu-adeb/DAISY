"""Early-warning flags, composite stress score and crisis mode (stocks).

Each flag is 0/1 per day and uses only data up to that close. Flags whose inputs
are missing are skipped (NaN), and the stress score is the share of *available*
flags that are on. Crisis mode switches on when the score reaches `enter` and off
when it falls below `exit` (hysteresis), with a minimum stay of `min_days`.

Optional inputs (as ticker CSVs in the stock data folder):
  VIX, VIX3M            volatility index and 3-month volatility index
  HYG, IEF              high-yield vs Treasury ETFs (credit stress proxy)
  XLU, XLY              defensive vs cyclical sectors
  sector ETFs           for the correlation flag (list in config)
Optional macro CSV (date + columns): T10Y2Y (10y-2y spread), HY_OAS (high-yield spread, %).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from abg.tradebook.common import indicators as ind


def _col(panel, t):
    return panel.close[t] if t in panel.close.columns else None


def warning_flags(panel, benchmark: str, p: dict, macro: pd.DataFrame | None = None) -> pd.DataFrame:
    c = panel.close[benchmark]
    idx = c.index
    f = {}
    sma200 = ind.sma(c, 200)
    f["trend_break"] = (c < sma200).astype(float).where(sma200.notna())
    vol_pct = ind.pct_rank(ind.realized_vol(c, 20), 252)
    f["vol_spike"] = (vol_pct >= p.get("vol_pct", 0.80)).astype(float).where(vol_pct.notna())
    hi = c.rolling(252, min_periods=60).max()
    dd = c / hi - 1
    f["drawdown_10"] = (dd <= -p.get("drawdown", 0.10)).astype(float).where(hi.notna())

    vix, vix3m = _col(panel, "VIX"), _col(panel, "VIX3M")
    if vix is not None:
        f["vix_high"] = (vix >= p.get("vix_level", 25)).astype(float).where(vix.notna())
        if vix3m is not None:
            ratio = vix / vix3m
            f["vix_inversion"] = (ratio > p.get("vix_ratio", 1.0)).astype(float).where(ratio.notna())

    hyg, ief = _col(panel, "HYG"), _col(panel, "IEF")
    if hyg is not None and ief is not None:
        cr = hyg / ief
        f["credit_stress"] = ((cr < ind.sma(cr, 50)) & (cr.pct_change(20) <= -p.get("credit_drop", 0.02))) \
            .astype(float).where(ind.sma(cr, 50).notna())

    xlu, xly = _col(panel, "XLU"), _col(panel, "XLY")
    if xlu is not None and xly is not None:
        dr = (xlu / xly).pct_change(63)
        f["defensive_lead"] = (dr >= p.get("defensive_lead", 0.05)).astype(float).where(dr.notna())

    stocks = [t for t in panel.tickers if t not in set(p.get("exclude_from_breadth", []))]
    if len(stocks) >= p.get("breadth_min_names", 20):
        cl = panel.close[stocks].where(panel.eligible[stocks])
        above = (cl > ind.sma(cl, 200)).where(cl.notna() & ind.sma(cl, 200).notna())
        breadth = above.mean(axis=1)
        f["breadth_weak"] = (breadth < p.get("breadth", 0.40)).astype(float).where(above.notna().sum(axis=1) >= 10)

    secs = [t for t in p.get("correlation_etfs", []) if t in panel.close.columns]
    if len(secs) >= 5:
        r = panel.close[secs].pct_change()
        n = len(secs)
        corr_avg = r.rolling(20, min_periods=20).corr()
        # average off-diagonal correlation per date
        vals = []
        for d in idx:
            try:
                m = corr_avg.loc[d].to_numpy()
                vals.append((np.nansum(m) - n) / (n * (n - 1)))
            except KeyError:
                vals.append(np.nan)
        ca = pd.Series(vals, index=idx)
        cp = ind.pct_rank(ca, 252)
        f["correlation_spike"] = (cp >= p.get("corr_pct", 0.90)).astype(float).where(cp.notna())

    if macro is not None:
        m = macro.reindex(idx).ffill()
        if "T10Y2Y" in m:
            f["curve_inverted"] = (m["T10Y2Y"] < 0).astype(float).where(m["T10Y2Y"].notna())
        if "HY_OAS" in m:
            ch = m["HY_OAS"] - m["HY_OAS"].shift(20)
            f["hy_spread_jump"] = (ch >= p.get("hy_jump", 0.75)).astype(float).where(ch.notna())

    flags = pd.DataFrame(f, index=idx)
    use = p.get("flags")           # optional whitelist from config
    if use:
        flags = flags[[c for c in flags.columns if c in use]]
    return flags


def stress_score(flags: pd.DataFrame) -> pd.Series:
    avail = flags.notna().sum(axis=1)
    return (flags.sum(axis=1) / avail.replace(0, np.nan)).fillna(0.0)


def crisis_mode(score: pd.Series, enter: float = 0.5, exit: float = 0.25, min_days: int = 10) -> pd.Series:
    on, days_on, out = False, 0, []
    for s in score.to_numpy():
        if on:
            days_on += 1
            if s < exit and days_on >= min_days:
                on, days_on = False, 0
        elif s >= enter:
            on, days_on = True, 0
        out.append(on)
    return pd.Series(out, index=score.index)


def forward_drawdown(close: pd.Series, horizon: int = 60) -> pd.Series:
    """Worst close over the next `horizon` days relative to today's close (<= 0)."""
    arr = close.to_numpy()
    out = np.full(len(arr), np.nan)
    for i in range(len(arr) - 1):
        j = min(i + horizon, len(arr) - 1)
        out[i] = np.nanmin(arr[i + 1:j + 1]) / arr[i] - 1
    return pd.Series(out, index=close.index)


def flag_study(flags: pd.DataFrame, close: pd.Series, horizon: int = 60, crash: float = -0.10) -> pd.DataFrame:
    """How useful is each flag as an early warning?
    target = benchmark falls >= |crash| below today's close within the next `horizon` days.
    precision = P(target | flag on); base = P(target); lift = precision / base;
    recall = P(flag on | target); fwd returns = mean forward return when the flag is on vs off."""
    fdd = forward_drawdown(close, horizon)
    tgt = (fdd <= crash).astype(float).where(fdd.notna())
    fwd20 = close.shift(-20) / close - 1
    fwd60 = close.shift(-horizon) / close - 1
    rows = []
    cols = list(flags.columns) + ["stress_score>=0.5"]
    score = stress_score(flags)
    for col in cols:
        fl = (score >= 0.5).astype(float) if col.startswith("stress") else flags[col]
        m = fl.notna() & tgt.notna()
        f_, t_ = fl[m], tgt[m]
        if f_.sum() == 0 or len(f_) < 100:
            continue
        base = t_.mean()
        prec = t_[f_ == 1].mean()
        rows.append({"flag": col, "days_on_share": float(f_.mean()), "base_rate": float(base),
                     "precision": float(prec), "lift": float(prec / base) if base > 0 else np.nan,
                     "recall": float(f_[t_ == 1].mean()) if t_.sum() else np.nan,
                     "fwd20_on": float(fwd20[m][f_ == 1].mean()), "fwd20_off": float(fwd20[m][f_ == 0].mean()),
                     f"fwd{horizon}_on": float(fwd60[m][f_ == 1].mean()), f"fwd{horizon}_off": float(fwd60[m][f_ == 0].mean())})
    if not rows:      # short history, or no flag was ever on: an empty table, not a KeyError on "lift"
        return pd.DataFrame(columns=["flag", "days_on_share", "base_rate", "precision", "lift", "recall",
                                     "fwd20_on", "fwd20_off", f"fwd{horizon}_on", f"fwd{horizon}_off"])
    return pd.DataFrame(rows).sort_values("lift", ascending=False)


def event_lead_table(flags: pd.DataFrame, events: pd.DataFrame, lookback: int = 120) -> pd.DataFrame:
    """For each event: was each flag on 20 and 5 trading days before the start, and how many trading days
    before the start did it first switch on (within `lookback` days, counting the latest unbroken run)?"""
    idx = flags.index
    score = stress_score(flags)
    rows = []
    for _, ev in events.iterrows():
        s = idx.searchsorted(ev["start"])
        if s <= 0 or s >= len(idx):
            continue
        row = {"event": ev["name"], "category": ev["category"], "start": ev["start"].date(),
               "score_t-20": float(score.iloc[max(s - 20, 0)]), "score_t-5": float(score.iloc[max(s - 5, 0)]),
               "score_t0": float(score.iloc[s])}
        for col in flags.columns:
            x = flags[col].to_numpy()
            lead = None
            k = s
            while k >= max(s - lookback, 0) and x[k] == 1:
                lead = s - k
                k -= 1
            row[f"{col}_lead_days"] = lead
        rows.append(row)
    return pd.DataFrame(rows)

"""Economic-crisis and geopolitical event catalog + windowed performance reports."""
from __future__ import annotations

import os

import pandas as pd

DEFAULT_EVENTS = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "events.csv")


def load_events(path: str | None = None) -> pd.DataFrame:
    path = path or DEFAULT_EVENTS
    ev = pd.read_csv(path, comment="#")
    ev["start"] = pd.to_datetime(ev["start"])
    ev["end"] = pd.to_datetime(ev["end"])
    if "pad_before" not in ev:
        ev["pad_before"] = 0
    if "pad_after" not in ev:
        ev["pad_after"] = 0
    return ev


def window_mask(index: pd.DatetimeIndex, start, end, pad_before: int = 0, pad_after: int = 0) -> pd.Series:
    """Trading-day window [start - pad_before days, end + pad_after days] on the given index."""
    idx = pd.DatetimeIndex(index)
    i0 = max(idx.searchsorted(pd.Timestamp(start)) - int(pad_before), 0)
    i1 = min(idx.searchsorted(pd.Timestamp(end), side="right") - 1 + int(pad_after), len(idx) - 1)
    m = pd.Series(False, index=idx)
    if i1 >= i0:
        m.iloc[i0:i1 + 1] = True
    return m


def event_report_equity(equity: pd.DataFrame, events: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """For each event window: return and max drawdown of each equity column (e.g. strategy, benchmark)."""
    rows = []
    for _, ev in events.iterrows():
        m = window_mask(equity.index, ev["start"], ev["end"], ev.get("pad_before", 0), ev.get("pad_after", 0))
        seg = equity.loc[m.values]
        if len(seg) < 2:
            continue
        row = {"event": ev["name"], "category": ev["category"], "start": seg.index[0].date(), "end": seg.index[-1].date(),
               "days": len(seg)}
        for c in cols:
            s = seg[c].dropna()
            if len(s) < 2:
                continue
            # include the move from the prior close into the window
            prev = equity[c].loc[:seg.index[0]].iloc[-2] if len(equity[c].loc[:seg.index[0]]) > 1 else s.iloc[0]
            path = pd.concat([pd.Series([prev]), s.reset_index(drop=True)])
            row[f"{c}_return"] = float(path.iloc[-1] / path.iloc[0] - 1)
            row[f"{c}_max_dd"] = float((path / path.cummax() - 1).min())
        rows.append(row)
    return pd.DataFrame(rows)


def event_report_r(daily: pd.DataFrame, events: pd.DataFrame, r_col: str = "net_R") -> pd.DataFrame:
    """For R-denominated daily results (futures): net R, trades and worst day per event window."""
    rows = []
    for _, ev in events.iterrows():
        m = window_mask(daily.index, ev["start"], ev["end"], ev.get("pad_before", 0), ev.get("pad_after", 0))
        seg = daily.loc[m.values]
        if seg.empty:
            continue
        cum = seg[r_col].cumsum()
        rows.append({"event": ev["name"], "category": ev["category"], "sessions": len(seg),
                     "net_R": float(seg[r_col].sum()), "trades": int(seg.get("trades", pd.Series(0)).sum()),
                     "worst_session_R": float(seg[r_col].min()),
                     "max_dd_R": float((cum - cum.cummax().clip(lower=0)).min())})
    return pd.DataFrame(rows)

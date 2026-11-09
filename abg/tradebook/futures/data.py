"""1-minute futures data -> sessions with the context each setup needs.

Input CSV: timestamp,open,high,low,close,volume. Timestamps are bar START times in the exchange's
local time (America/New_York by default). If they carry a UTC offset or `tz_in` is set, they are
converted. Use a back-adjusted continuous series for multi-day indicators if you have one; intraday
setups only use same-session prices and the prior session's levels.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Session:
    date: pd.Timestamp
    rth: pd.DataFrame                 # 1-min RTH bars, index = bar start time
    prev_close: float
    prev_high: float
    prev_low: float
    on_high: float                    # overnight (prior RTH close -> RTH open) high/low; NaN if no bars
    on_low: float
    adr20: float                      # average RTH range (points) of the prior 20 sessions
    atr14: float                      # daily ATR (points) of the prior sessions
    prev_er: float                    # prior session efficiency ratio on 5-min closes
    bias: float                       # sign of blended 20/60-session RTH return (+1/-1/0), prior sessions only


def load_minutes(path: str, tz_in: str | None = None, tz: str = "America/New_York") -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = "timestamp" if "timestamp" in df.columns else df.columns[0]
    ts = pd.to_datetime(df[tcol], utc=bool(tz_in) or None)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(tz).dt.tz_localize(None)
    df.index = ts
    df = df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
    return df[~df.index.duplicated()]


def build_sessions(m: pd.DataFrame, rth_start="09:30", rth_end="16:00") -> list[Session]:
    t = m.index
    tod = t.strftime("%H:%M")
    rth_mask = (tod >= rth_start) & (tod < rth_end)
    rth = m[rth_mask]
    days = rth.groupby(rth.index.normalize())
    daily = days.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    daily = daily[days.size() >= 300]          # skip half days / bad sessions
    rng = daily["high"] - daily["low"]
    pc = daily["close"].shift(1)
    tr = pd.concat([rng, (daily["high"] - pc).abs(), (daily["low"] - pc).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().shift(1)
    adr20 = rng.rolling(20, min_periods=20).mean().shift(1)
    r20 = daily["close"].pct_change(20)
    r60 = daily["close"].pct_change(60)
    bias = np.sign(0.5 * r20.fillna(0) + 0.5 * r60.fillna(0)).where(r60.notna(), 0.0).shift(1)
    # prior-session efficiency ratio on 5-min closes
    er = {}
    for d, g in days:
        c5 = g["close"].resample("5min", origin="start_day", offset="9h30min").last().dropna()
        path = c5.diff().abs().sum()
        er[d] = abs(c5.iloc[-1] - c5.iloc[0]) / path if path > 0 else np.nan
    er = pd.Series(er).reindex(daily.index).shift(1)

    sessions = []
    dates = list(daily.index)
    for k, d in enumerate(dates):
        if k == 0:
            continue
        prev = dates[k - 1]
        g = rth[(rth.index >= d) & (rth.index < d + pd.Timedelta(days=1))]
        on = m[(m.index >= prev + pd.Timedelta(hours=16)) & (m.index < g.index[0])]
        sessions.append(Session(
            date=d, rth=g, prev_close=float(daily.at[prev, "close"]), prev_high=float(daily.at[prev, "high"]),
            prev_low=float(daily.at[prev, "low"]),
            on_high=float(on["high"].max()) if len(on) else np.nan, on_low=float(on["low"].min()) if len(on) else np.nan,
            adr20=float(adr20.get(d, np.nan)), atr14=float(atr14.get(d, np.nan)), prev_er=float(er.get(d, np.nan)),
            bias=float(bias.get(d, 0.0))))
    return sessions, daily


def resample(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate 1-min RTH bars into N-minute bars aligned to the session open; index = bar start.
    Also returns column 'last_i' = position (in the 1-min frame) of the bar's final minute."""
    g = bars.copy()
    g["i"] = np.arange(len(g))
    r = g.resample(f"{minutes}min", origin="start_day", offset="9h30min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "i": "max"}).dropna()
    return r.rename(columns={"i": "last_i"})


def load_econ_events(path: str | None) -> pd.DataFrame:
    if not path:
        return pd.DataFrame(columns=["datetime", "name", "minutes_before", "minutes_after"])
    ev = pd.read_csv(path, comment="#")
    ev["datetime"] = pd.to_datetime(ev["datetime"])
    if "minutes_before" not in ev:
        ev["minutes_before"] = 5
    if "minutes_after" not in ev:
        ev["minutes_after"] = 5
    return ev

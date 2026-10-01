"""Adaptive opening-range mode: follow the first breakout, fade it, or stand aside.

On the MNQ 5-minute data the first close outside the 15-minute opening range was a coin flip on
average, but *which way* it paid ran in streaks: some weeks breakouts ran, other weeks they reversed.
So every morning the bot replays the last few sessions twice in the background - once following the
first breakout, once fading it (same stop, Target 1 and final) - and trades today in whichever mode
earned more R over that window, or stands aside when neither did.  This is the bot keeping up with the
market's current character rather than assuming one.

The replay (``shadow``) uses 5-minute bars and simple management (stop at entry -/+ k x opening ATR,
half off at 1R with the stop to break-even, rest at the final R, flat at 15:50; a bar touching stop
and target counts as the stop).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd


def _to5(rth: pd.DataFrame) -> pd.DataFrame:
    if len(rth) < 2:
        return rth
    step = (rth.index[1] - rth.index[0]).total_seconds()
    if step >= 300:
        return rth
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in rth.columns:
        agg["volume"] = "sum"
    return rth.resample("5min", label="left", closed="left").agg(agg).dropna(subset=["close"])


def opening_range(rth5: pd.DataFrame, minutes: int = 15) -> tuple[float, float, float] | None:
    end = (datetime(2000, 1, 1, 9, 30) + timedelta(minutes=minutes)).time()
    o = rth5[[t.time() < end for t in rth5.index]]
    if o.empty:
        return None
    return float(o["high"].max()), float(o["low"].min()), float((o["high"] - o["low"]).mean())


def _sim(r: pd.DataFrame, t, side: int, e: float, risk: float, t1_r: float, final_r: float,
         flat: time = time(15, 50), tick: float = 0.25) -> float:
    stop, t1, fin = e - side * risk, e + side * t1_r * risk, e + side * final_r * risk
    hit, bank = False, 0.0
    for tt, x in r[r.index > t].iterrows():
        fav, adv = (x["high"], x["low"]) if side > 0 else (x["low"], x["high"])
        if (adv - stop) * side <= 0:
            px = stop - side * tick
            return ((bank + (px - e) * side * 0.5) if hit else (px - e) * side) / risk
        if not hit and (fav - t1) * side >= 0:
            hit, bank, stop = True, (t1 - e) * side * 0.5, e
        if (fav - fin) * side >= 0:
            return ((bank + (fin - e) * side * 0.5) if hit else (fin - e) * side) / risk
        if tt.time() >= flat:
            px = x["close"]
            return ((bank + (px - e) * side * 0.5) if hit else (px - e) * side) / risk
    px = float(r["close"].iloc[-1])
    return ((bank + (px - e) * side * 0.5) if hit else (px - e) * side) / risk


def shadow(rth: pd.DataFrame, k: float = 0.8, t1_r: float = 1.0, final_r: float = 2.0, or_minutes: int = 15,
           last: time = time(10, 30)) -> dict:
    """R for following and for fading the session's first opening-range breakout (None = no breakout)."""
    r5 = _to5(rth)
    rng = opening_range(r5, or_minutes)
    if rng is None:
        return {"follow": None, "fade": None}
    hi, lo, atr = rng
    start = (datetime(2000, 1, 1, 9, 30) + timedelta(minutes=or_minutes)).time()
    for t, x in r5[[start <= t.time() < last for t in r5.index]].iterrows():
        s = 1 if x["close"] > hi else -1 if x["close"] < lo else 0
        if s:
            risk = max(k * atr, 1.0)
            return {"follow": round(_sim(r5, t, s, float(x["close"]), risk, t1_r, final_r), 3),
                    "fade": round(_sim(r5, t, -s, float(x["close"]), risk, t1_r, final_r), 3),
                    "time": t.strftime("%H:%M"), "dir": "up" if s > 0 else "down"}
    return {"follow": None, "fade": None}


def choose_mode(history: list[dict], lookback: int = 3) -> tuple[str, dict]:
    """``history``: shadow results of prior sessions, oldest first.  Returns (mode, scores)."""
    recent = history[-lookback:]
    f = sum(h.get("follow") or 0.0 for h in recent)
    d = sum(h.get("fade") or 0.0 for h in recent)
    scores = {"follow": round(f, 2), "fade": round(d, 2), "sessions": len(recent)}
    if len(recent) < lookback or max(f, d) <= 0:
        return "off", scores
    return ("fade" if d >= f else "follow"), scores

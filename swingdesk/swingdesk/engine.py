"""Configurable execution engine used for both research and live signal management.

`Plan` describes HOW a signal is traded (entry style, exits, stops). The research
script sweeps plans over the historical signals; the live scanner uses the winning
plan to tell you when to enter, scale out and exit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from .backtest import MARKET_OPEN, atr


@dataclass(frozen=True)
class Plan:
    name: str = "as_posted"
    entry: str = "limit"          # limit | confirm  (confirm = wait for a reclaim bar inside/above zone)
    same_day: bool = False        # allow fills on the bar the post was made during (ambiguous timing)
    tol_pct: float = 0.0          # fill if price comes within tol% of the zone edge
    tp1_r: float | None = None    # first take-profit in R (None = posted target only)
    tp1_frac: float = 1.0         # fraction closed at tp1 (rest rides to posted target)
    be_after_tp1: bool = True     # move stop to breakeven once tp1 is hit
    stop: str = "posted"          # posted | intraday | atr
    atr_mult: float = 1.0         # for stop == "atr": zone_lo - atr_mult*ATR
    time_stop: int | None = None  # bars after fill; exit at close if still open
    fill_window: int = 30
    min_wait: int = 0             # ignore fills on the first N eligible bars (no knife-catching on post)
    # spin-off PATCH 7: chase.  0 = off (the audited behaviour).  > 0: if, before the zone is touched, a session closes
    # beyond the zone toward the target by at most ``chase_atr`` x ATR (and the target is still more than half the
    # zone-to-target distance away), buy the next open, same stop.  Only within the first ``chase_bars`` sessions.
    chase_atr: float = 0.0
    chase_bars: int = 5


def _eligible_start(px, posted: datetime, same_day: bool):
    day = pd.Timestamp(posted.date())
    i = px.index.searchsorted(day)
    if i < len(px) and px.index[i] == day and posted.time() >= MARKET_OPEN and not same_day:
        i += 1
    return i if i < len(px) else None


def run(sig: dict, px: pd.DataFrame, plan: Plan) -> dict:
    """Long-only and short-aware replay. Returns dict with status, R, etc."""
    long = sig["direction"] == "long"
    sgn = 1 if long else -1
    t_post = float(sig["target_lo"] if long else sig["target_hi"])
    stop = float(sig["stop"])
    close_stop = (sig["stop_basis"] == "close") if plan.stop == "posted" else False
    start = _eligible_start(px, sig["posted"], plan.same_day)
    if start is None:
        return {"status": "PENDING"}
    a = atr(px)
    etype = sig["entry_type"]
    zone_hi = float(sig["entry_hi"]) if etype == "zone" else None
    zone_lo = float(sig["entry_lo"]) if etype == "zone" else None
    edge = (zone_hi if long else zone_lo) if etype == "zone" else None
    if edge is not None and plan.tol_pct:
        edge = edge * (1 + sgn * plan.tol_pct / 100)

    fill_i = fill_px = None
    touched_i = None
    chased = False
    for i in range(start, min(len(px), start + plan.fill_window)):
        o, h, l, c = px.iloc[i][["open", "high", "low", "close"]]
        if etype == "cmp":
            fill_i, fill_px = i, o
            break
        if etype == "close_above":
            if c > float(sig["entry_lo"]):
                fill_i, fill_px = i, c
                break
            continue
        touch = ((l <= edge) if long else (h >= edge)) and (i - start >= plan.min_wait)
        ran = (h >= t_post) if long else (l <= t_post)
        broke = (l <= stop) if long else (h >= stop)
        if plan.entry == "limit":
            if touch:
                fill_i, fill_px = i, (min(o, edge) if long else max(o, edge))
                break
        else:  # confirm: after a touch, need a close back inside/above the zone with a bullish body
            if touched_i is None and touch:
                touched_i = i
            if touched_i is not None:
                ok = (c > o and c >= (zone_lo if long else zone_hi) and c > px.iloc[i - 1]["close"]) if long else \
                     (c < o and c <= zone_hi and c < px.iloc[i - 1]["close"])
                if ok and i + 1 < len(px):
                    fill_i, fill_px = i + 1, px.iloc[i + 1]["open"]
                    break
        if (ran and touched_i is None and fill_i is None) or (broke and fill_i is None and
                                                              (sig["stop_basis"] != "close" or c < stop)):
            return {"status": "NOFILL_RAN" if ran else "NOFILL_INVALID"}
        if plan.chase_atr > 0 and plan.entry == "limit" and etype == "zone" and i - start < plan.chase_bars \
                and 0 < sgn * (c - edge) <= plan.chase_atr * a.iloc[i] and sgn * (t_post - c) > 0.5 * sgn * (t_post - edge):
            if i + 1 >= len(px):
                return {"status": "PENDING", "chase_next_open": True, "chase_close": float(c)}
            nxt = px.iloc[i + 1]["open"]
            if sgn * (t_post - nxt) > 0.25 * sgn * (t_post - edge):       # not if it gaps most of the way to the target
                fill_i, fill_px, chased = i + 1, nxt, True
                break
    if fill_i is None:
        return {"status": "PENDING"}

    if plan.stop == "atr" and etype == "zone":
        stop = (zone_lo - plan.atr_mult * a.iloc[fill_i - 1]) if long else (zone_hi + plan.atr_mult * a.iloc[fill_i - 1])
    risk = sgn * (fill_px - stop)
    if risk <= 0:
        return {"status": "LOSS", "r": -1.0, "fill_i": fill_i}
    tp1 = fill_px + sgn * plan.tp1_r * risk if plan.tp1_r else None
    if tp1 is not None and sgn * (tp1 - t_post) > 0:
        tp1 = None  # posted target is closer; just use it
    open_frac, realized, cur_stop = 1.0, 0.0, stop
    took_tp1 = False
    for i in range(fill_i, len(px)):
        o, h, l, c = px.iloc[i][["open", "high", "low", "close"]]
        first = i == fill_i
        at_open = first and (etype == "cmp" or chased or math.isclose(fill_px, o) or plan.entry == "confirm")
        fav_ok = (not first) or at_open
        # stops first (conservative)
        stop_hit = (c < cur_stop if long else c > cur_stop) if (close_stop and cur_stop == stop) \
            else ((l <= cur_stop) if long else (h >= cur_stop))
        stop_px = c if (close_stop and cur_stop == stop) else (o if ((o <= cur_stop) if long else (o >= cur_stop)) and not first else cur_stop)
        # intraday ambiguity: if stop (intraday) and target both in bar -> stop wins
        if stop_hit and not (close_stop and cur_stop == stop):
            realized += open_frac * sgn * (stop_px - fill_px) / risk
            return _out("WIN" if realized > 0 else ("SCRATCH" if took_tp1 and abs(realized) < 1e-9 else "LOSS"),
                        realized, fill_i, i, px, fill_px, chased=chased)
        if fav_ok and tp1 is not None and not took_tp1 and ((h >= tp1) if long else (l <= tp1)):
            realized += plan.tp1_frac * plan.tp1_r
            open_frac -= plan.tp1_frac
            took_tp1 = True
            if plan.be_after_tp1:
                cur_stop = fill_px
            if open_frac <= 1e-9:
                return _out("WIN", realized, fill_i, i, px, fill_px, chased=chased)
        if fav_ok and ((h >= t_post) if long else (l <= t_post)):
            ex = o if ((o >= t_post) if long else (o <= t_post)) and not first else t_post
            realized += open_frac * sgn * (ex - fill_px) / risk
            return _out("WIN" if realized > 0 else "LOSS", realized, fill_i, i, px, fill_px, chased=chased)
        if stop_hit:  # close-basis stop
            realized += open_frac * sgn * (stop_px - fill_px) / risk
            return _out("WIN" if realized > 0 else "LOSS", realized, fill_i, i, px, fill_px, chased=chased)
        if plan.time_stop and i - fill_i >= plan.time_stop:
            realized += open_frac * sgn * (c - fill_px) / risk
            return _out("WIN" if realized > 0 else "LOSS", realized, fill_i, i, px, fill_px, timed=True, chased=chased)
    mtm = realized + open_frac * sgn * (px.iloc[-1]["close"] - fill_px) / risk
    return {"status": "OPEN", "r": round(mtm, 3), "fill_i": fill_i, "took_tp1": took_tp1,
            "fill_px": fill_px, "fill_date": str(px.index[fill_i].date()), **({"chased": True} if chased else {})}


def _out(status, r, fi, xi, px, fill_px, timed=False, chased=False):
    return {"status": status, "r": round(float(r), 3), "fill_i": fi, "exit_i": xi, "bars": xi - fi,
            "fill_px": fill_px, "fill_date": str(px.index[fi].date()), "exit_date": str(px.index[xi].date()),
            "timed_out": timed, **({"chased": True} if chased else {})}


def summarize(results: list[dict]) -> dict:
    closed = [r for r in results if r["status"] in ("WIN", "LOSS", "SCRATCH")]
    wins = [r for r in closed if r["status"] == "WIN"]
    rs = np.array([r["r"] for r in closed]) if closed else np.array([0.0])
    filled = [r for r in results if "fill_i" in r]
    return {
        "signals": len(results),
        "filled": len(filled),
        "closed": len(closed),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else np.nan,
        "avg_R": round(float(rs.mean()), 3),
        "sum_R": round(float(rs.sum()), 2),
        "profit_factor": round(float(rs[rs > 0].sum() / -rs[rs < 0].sum()), 2) if (rs < 0).any() else np.inf,
        "open": sum(1 for r in results if r["status"] == "OPEN"),
        "missed_ran": sum(1 for r in results if r["status"] == "NOFILL_RAN"),
    }

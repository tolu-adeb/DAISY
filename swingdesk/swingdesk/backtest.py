"""Bar-by-bar replay of swing signals against daily OHLC.

Conservative by design (we would rather understate the edge than overstate it):
  * A signal can only fill on bars that started after it was posted
    (posted before 09:30 ET -> same day eligible, otherwise next session).
  * Zone orders are limit buys at the top of the zone (fill = min(open, zone_hi)).
  * If target and stop are both touched inside one bar, the trade is scored a loss.
  * On the fill bar only the stop is checked unless the fill happened at the open.
  * "Daily close" stops are checked on the close; others on the intraday low.
  * Target = the first (lower) target; a hit of the upper target is reported separately.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, time

import numpy as np
import pandas as pd

MARKET_OPEN = time(9, 30)
FILL_WINDOW = 30          # trading days a zone order stays live


def load_prices(path: str, year: int = 2026) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"d": str})
    # research cache stores MMDD (single year); the live cache stores YYYYMMDD
    d = df["d"].where(df["d"].str.len() == 8, str(year) + df["d"].str.zfill(4))
    df["date"] = pd.to_datetime(d, format="%Y%m%d")
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df.sort_values("date").set_index("date")[["open", "high", "low", "close", "volume"]]


@dataclass
class Result:
    status: str                     # WIN | LOSS | OPEN | NOFILL_RAN | NOFILL_INVALID | PENDING
    fill_date: str | None = None
    fill_px: float | None = None
    exit_date: str | None = None
    exit_px: float | None = None
    r_multiple: float | None = None
    pnl_pct: float | None = None
    bars_to_fill: int | None = None
    bars_held: int | None = None
    mfe_r: float | None = None      # max favourable excursion while open, in R
    mae_r: float | None = None      # max adverse excursion while open, in R
    hit_upper_target: bool = False
    last_px: float | None = None
    notes: list = field(default_factory=list)


def _first_eligible(px: pd.DataFrame, posted: datetime) -> int | None:
    day = pd.Timestamp(posted.date())
    idx = px.index.searchsorted(day)
    if idx >= len(px):
        return None
    if px.index[idx] == day and posted.time() >= MARKET_OPEN:
        idx += 1
    return idx if idx < len(px) else None


def simulate(sig: dict, px: pd.DataFrame, fill_window: int = FILL_WINDOW) -> Result:
    long = sig["direction"] == "long"
    sgn = 1 if long else -1
    stop = float(sig["stop"])
    t1 = float(sig["target_lo"] if long else sig["target_hi"])
    t2 = float(sig["target_hi"] if long else sig["target_lo"])
    close_stop = sig["stop_basis"] == "close"
    start = _first_eligible(px, sig["posted"])
    if start is None:
        return Result("PENDING", notes=["no bars after post"])

    # ---------------- entry ----------------
    fill_i = fill_px = None
    etype = sig["entry_type"]
    for k, i in enumerate(range(start, min(len(px), start + fill_window))):
        o, h, l, c = px.iloc[i][["open", "high", "low", "close"]]
        if etype == "cmp":
            fill_i, fill_px = i, o
            break
        if etype == "close_above":
            if c > float(sig["entry_lo"]):
                fill_i, fill_px = i, c
                break
            continue
        zone_edge = float(sig["entry_hi"] if long else sig["entry_lo"])
        touched = (l <= zone_edge) if long else (h >= zone_edge)
        ran = (h >= t1) if long else (l <= t1)
        broke = (c < stop if close_stop else l <= stop) if long else (c > stop if close_stop else h >= stop)
        if touched:
            fill_i = i
            fill_px = min(o, zone_edge) if long else max(o, zone_edge)
            break
        if ran:
            return Result("NOFILL_RAN", notes=[f"target reached {px.index[i].date()} before entry"],
                          last_px=float(px.iloc[-1]["close"]))
        if broke:
            return Result("NOFILL_INVALID", notes=["stop level broken before entry"],
                          last_px=float(px.iloc[-1]["close"]))
    if fill_i is None:
        return Result("PENDING", last_px=float(px.iloc[-1]["close"]))

    risk = sgn * (fill_px - stop)
    if risk <= 0:
        return Result("LOSS", fill_date=str(px.index[fill_i].date()), fill_px=fill_px,
                      notes=["filled through stop"], r_multiple=-1.0)
    res = Result("OPEN", fill_date=str(px.index[fill_i].date()), fill_px=round(fill_px, 4),
                 bars_to_fill=fill_i - start)
    mfe = mae = 0.0
    filled_at_open = etype == "cmp" or math.isclose(fill_px, px.iloc[fill_i]["open"])

    # ---------------- manage ----------------
    for i in range(fill_i, len(px)):
        o, h, l, c = px.iloc[i][["open", "high", "low", "close"]]
        fav = sgn * ((h if long else l) - fill_px) / risk
        adv = sgn * ((l if long else h) - fill_px) / risk
        mfe, mae = max(mfe, fav), min(mae, adv)
        first = i == fill_i
        hit_t = (h >= t1) if long else (l <= t1)
        hit_s_intraday = (l <= stop) if long else (h >= stop)
        close_breach = (c < stop) if long else (c > stop)
        gap_t = (o >= t1) if long else (o <= t1)
        gap_s = (o <= stop) if long else (o >= stop)

        exit_px = status = None
        if not first and gap_t:
            exit_px, status = o, "WIN"
        elif not first and not close_stop and gap_s:
            exit_px, status = o, "LOSS"
        elif not close_stop and hit_s_intraday and (hit_t or first):
            exit_px, status = stop, "LOSS"          # ambiguous bar -> loss
        elif not close_stop and hit_s_intraday:
            exit_px, status = stop, "LOSS"
        elif hit_t and (not first or filled_at_open):
            exit_px, status = t1, "WIN"
        elif close_stop and close_breach:
            exit_px, status = c, "LOSS"
        if status:
            res.status = status
            res.exit_date = str(px.index[i].date())
            res.exit_px = round(float(exit_px), 4)
            res.r_multiple = round(sgn * (exit_px - fill_px) / risk, 3)
            res.pnl_pct = round(100 * sgn * (exit_px - fill_px) / fill_px, 2)
            res.bars_held = i - fill_i
            res.hit_upper_target = bool(((px.iloc[fill_i:i + 1]["high"] >= t2).any()) if long
                                        else ((px.iloc[fill_i:i + 1]["low"] <= t2).any()))
            break
    res.mfe_r, res.mae_r = round(mfe, 2), round(mae, 2)
    res.last_px = float(px.iloc[-1]["close"])
    if res.status == "OPEN":
        res.r_multiple = round(sgn * (res.last_px - fill_px) / risk, 3)   # mark-to-market
        res.pnl_pct = round(100 * sgn * (res.last_px - fill_px) / fill_px, 2)
        res.bars_held = len(px) - 1 - fill_i
    return res


# ---------------------------------------------------------------- features
def atr(px: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = px["close"].shift()
    tr = pd.concat([px["high"] - px["low"], (px["high"] - pc).abs(), (px["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=5).mean()


def features_at(sig: dict, px: pd.DataFrame, spy: pd.DataFrame) -> dict:
    """Everything here uses data available when the signal was posted (no look-ahead)."""
    day = pd.Timestamp(sig["posted"].date())
    hist = px[px.index < day] if sig["posted"].time() < MARKET_OPEN else px[px.index <= day]
    spy_h = spy[spy.index <= hist.index[-1]] if len(hist) else spy.iloc[:0]
    if len(hist) < 15:
        return {}
    last = hist["close"].iloc[-1]
    a = atr(hist).iloc[-1]
    sma20 = hist["close"].rolling(20, min_periods=10).mean()
    ret20 = last / hist["close"].iloc[max(0, len(hist) - 21)] - 1
    spy_ret20 = spy_h["close"].iloc[-1] / spy_h["close"].iloc[max(0, len(spy_h) - 21)] - 1
    spy_sma20 = spy_h["close"].rolling(20).mean().iloc[-1]
    long = sig["direction"] == "long"
    if sig["entry_type"] == "zone":
        e_mid = (float(sig["entry_lo"]) + float(sig["entry_hi"])) / 2
        e_edge = float(sig["entry_hi"] if long else sig["entry_lo"])
    else:
        e_mid = e_edge = last if sig["entry_type"] == "cmp" else float(sig["entry_lo"])
    t1 = float(sig["target_lo"] if long else sig["target_hi"])
    stop = float(sig["stop"])
    risk = abs(e_mid - stop)
    vol20 = hist["volume"].tail(20).mean()
    return {
        "px_at_post": round(last, 4),
        "atr_pct": round(100 * a / last, 2),
        "dist_to_zone_atr": round((last - e_edge) / a * (1 if long else -1), 2),
        "stop_dist_atr": round(risk / a, 2),
        "target_dist_atr": round(abs(t1 - e_mid) / a, 2),
        "planned_rr": round(abs(t1 - e_mid) / risk, 2) if risk else np.nan,
        "above_sma20": bool(last > sma20.iloc[-1]),
        "sma20_slope_5d": round(100 * (sma20.iloc[-1] / sma20.iloc[-6] - 1), 2) if len(sma20.dropna()) > 6 else np.nan,
        "ret20": round(100 * ret20, 2),
        "rs20_vs_spy": round(100 * (ret20 - spy_ret20), 2),
        "spy_above_sma20": bool(spy_h["close"].iloc[-1] > spy_sma20),
        "spy_ret20": round(100 * spy_ret20, 2),
        "dollar_vol_m": round(vol20 * 1000 * last / 1e6, 1),
    }

"""Systematic version of the House of Traders setup, so the desk can generate its own ideas.

The posts are written in smart-money-concepts language; stripped down, the repeatable
part is: an uptrend that just printed a break of structure (close above the last swing
high), followed by a pullback into the 0.5-0.618 retracement of the impulse leg.
Stop below the 0.786 level, target = retest of the leg high.

The output rows have the same shape as data/signals.csv, so the same grader, engine
and monitor handle Discord ideas and scanner ideas identically.
"""
from __future__ import annotations

import pandas as pd

from .backtest import atr


def pivots(px: pd.DataFrame, k: int = 3) -> tuple[pd.Series, pd.Series]:
    """Fractal swing highs/lows, confirmed k bars later (no look-ahead when used at bar i)."""
    hi, lo = px["high"], px["low"]
    ph = hi[(hi == hi.rolling(2 * k + 1, center=True).max())]
    pl = lo[(lo == lo.rolling(2 * k + 1, center=True).min())]
    return ph, pl


def find_setups(sym: str, px: pd.DataFrame, k: int = 3, min_leg_atr: float = 3.0,
                lookback: int = 60, start: str | None = None) -> list[dict]:
    """Walk forward bar by bar; emit a setup on the bar a BOS is confirmed."""
    out = []
    a = atr(px)
    ph, pl = pivots(px, k)
    last_emit_leg = None
    for i in range(30, len(px)):
        t = px.index[i]
        if start and t < pd.Timestamp(start):
            continue
        conf_cut = px.index[i - k]                    # pivots are only known k bars later
        hs, ls = ph[ph.index <= conf_cut], pl[pl.index <= conf_cut]
        if len(hs) < 2 or len(ls) < 2:
            continue
        last_hi_t, last_hi = hs.index[-1], hs.iloc[-1]
        c = px["close"].iloc[i]
        prev_c = px["close"].iloc[i - 1]
        # BOS: today's close takes out the last confirmed swing high, yesterday's did not
        if not (c > last_hi and prev_c <= last_hi):
            continue
        # higher-low structure: most recent swing low above the one before it
        if not (ls.iloc[-1] > ls.iloc[-2]):
            continue
        origin_t = ls.index[-1] if ls.index[-1] < t else ls.index[-2]
        origin = float(px.loc[origin_t, "low"])
        leg_hi = float(px["high"].iloc[px.index.get_loc(origin_t):i + 1].max())
        leg = leg_hi - origin
        if leg < min_leg_atr * a.iloc[i] or (last_emit_leg and abs(origin - last_emit_leg) < 1e-9):
            continue
        z_hi = leg_hi - 0.5 * leg
        z_lo = leg_hi - 0.618 * leg
        stop = leg_hi - 0.786 * leg - 0.25 * a.iloc[i]
        if not (stop < z_lo < z_hi < c):
            continue
        last_emit_leg = origin
        out.append({
            "posted_et": f"{t.date()} 16:30", "author": "scanner", "symbol": sym, "direction": "long",
            "entry_type": "zone", "entry_lo": round(z_lo, 4), "entry_hi": round(z_hi, 4),
            "target_lo": round(leg_hi, 4), "target_hi": round(leg_hi, 4), "stop": round(stop, 4),
            "stop_basis": "close", "levels_source": "scanner",
            "note": f"BOS over {last_hi:.2f} (swing {last_hi_t.date()}); leg {origin:.2f}->{leg_hi:.2f}",
        })
    return out

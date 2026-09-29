"""Shorter-timeframe entry confirmation for zone (pullback) entries.

Touching the zone isn't enough on its own: price can slice straight through it.  With
``ABG_EXT_CONFIRM_TIMEFRAME=1h`` (default) a long zone entry waits until a completed 1-hour bar
shows buyers stepping in near the zone:

    * a bullish bar that closes above the prior bar's high (outside-reversal / engulfing), or
    * a close back above the 9-period EMA after being below it,

with the bar's low within the zone (or at most 0.2% above it).  Shorts mirror this.  If intraday
data isn't available the entry proceeds (and says so); after ``ABG_EXT_CONFIRM_MAX_WAIT_HOURS`` in
the zone without a signal it enters anyway, so a slow grind doesn't miss the trade.
"""
from __future__ import annotations

import time

import pandas as pd

from ..errors import ABGError

TF = {"1h": ("1h", "1mo", 3600), "15m": ("15m", "5d", 900), "30m": ("30m", "1mo", 1800)}


async def confirm_entry(tracker, idea, price: float) -> tuple[bool, str]:
    s = tracker.s
    tf = (s.ext_confirm_timeframe or "none").lower()
    if tf not in TF or idea.entry_type != "zone":
        return True, ""
    now = time.time()
    first = idea.flags.setdefault("zone_touched_at", now)
    waited_h = (now - first) / 3600
    if waited_h >= s.ext_confirm_max_wait_hours:
        return True, f"no {tf} reversal after {waited_h:.0f}h in the zone: entering on time"
    interval, period, secs = TF[tf]
    cache = tracker.__dict__.setdefault("_intraday", {})
    hit = cache.get((idea.symbol, tf))
    df = None
    if hit and now - hit[0] < 300:
        df = hit[1]
    else:
        try:
            df = (await tracker.engine.history(idea.symbol, period, interval, ttl=240)).value.df
        except ABGError:
            df = None
        cache[(idea.symbol, tf)] = (now, df)
    if df is None or len(df) < 12:
        return True, f"{tf} data unavailable: entering without confirmation"
    last_ts = pd.Timestamp(df.index[-1])
    last_ts = last_ts.tz_localize("UTC") if last_ts.tzinfo is None else last_ts
    if last_ts.timestamp() + secs > now:                 # drop the bar that is still forming
        df = df.iloc[:-1]
    if len(df) < 11:
        return True, f"{tf} data too short: entering without confirmation"
    c, o, h, lo = df["close"], df["open"], df["high"], df["low"]
    ema = c.ewm(span=9, adjust=False).mean()
    zlo, zhi = idea.flags.get("zone_low", idea.entry_low), idea.entry_high
    b, p = -1, -2
    if idea.long:
        near = lo.iloc[b] <= zhi * 1.002 or lo.iloc[p] <= zhi * 1.002
        engulf = c.iloc[b] > o.iloc[b] and c.iloc[b] > h.iloc[p]
        reclaim = c.iloc[p] < ema.iloc[p] and c.iloc[b] > ema.iloc[b]
    else:
        near = h.iloc[b] >= zlo * 0.998 or h.iloc[p] >= zlo * 0.998
        engulf = c.iloc[b] < o.iloc[b] and c.iloc[b] < lo.iloc[p]
        reclaim = c.iloc[p] > ema.iloc[p] and c.iloc[b] < ema.iloc[b]
    if near and (engulf or reclaim):
        what = ("closed beyond the prior bar's " + ("high" if idea.long else "low")) if engulf else \
            ("reclaimed" if idea.long else "lost") + " the 9-EMA"
        return True, f"{tf} confirmation: the last {tf} bar {what} at {c.iloc[b]:,.2f}"
    return False, (f"waiting for {tf} confirmation: price is in the zone but no {tf} bar has "
                   f"{'closed above the prior high or reclaimed the 9-EMA' if idea.long else 'closed below the prior low or lost the 9-EMA'} "
                   f"yet ({waited_h:.1f}h of {s.ext_confirm_max_wait_hours:g}h max wait)")

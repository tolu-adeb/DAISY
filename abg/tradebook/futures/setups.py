"""The six intraday setups. Each function reads one session and returns trades (shadow trades: every
setup is simulated every session, independently, so the adaptive layer can learn from all of them).

Families:  trend = orb, cttd, vwap_pullback      fade = orx, rcttd, vwap_fade
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from abg.tradebook.futures.data import Session, resample
from abg.tradebook.futures.sim import Costs, Plan, simulate, simulate_flip_trade

FAMILY = {"orb": "trend", "cttd": "trend", "vwap_pullback": "trend",
          "orx": "fade", "rcttd": "fade", "vwap_fade": "fade"}


def _minute_index(rth: pd.DataFrame, hhmm: str) -> int:
    """Position of the first 1-min bar starting at or after hh:mm (len(rth) if none)."""
    t = rth.index[0].normalize() + pd.Timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[3:]))
    return int(rth.index.searchsorted(t))


def _arrays(rth):
    return (rth["open"].to_numpy(), rth["high"].to_numpy(), rth["low"].to_numpy(), rth["close"].to_numpy(),
            rth["volume"].to_numpy())


def _plan_kwargs(p: dict) -> dict:
    return dict(pt1_r=p.get("pt1_r", 1.0), pt1_frac=p.get("pt1_frac", 0.5), target_r=p.get("target_r", 2.0),
                be_after_pt1=p.get("be_after_pt1", True))


def opening_range(rth: pd.DataFrame, minutes: int):
    n = _minute_index(rth, "09:30") + minutes
    seg = rth.iloc[:n]
    return float(seg["high"].max()), float(seg["low"].min()), n


# ----------------------------------------------------------------------------- 1. ORB
def orb(s: Session, p: dict, costs: Costs, flat_i: int) -> list[dict]:
    """Opening-range breakout. OR = first `or_minutes`. A 5-min close above the OR high (below the low)
    between OR end and `last_entry` triggers a long (short) at the next 1-min open. Stop = OR midpoint
    (stop_mode 'mid') or the opposite side ('opposite'). Skip the day if OR width / ADR20 is outside
    [min_width_adr, max_width_adr]. At most one trade per direction."""
    rth = s.rth
    o, h, l, c, _ = _arrays(rth)
    orh, orl, n_or = opening_range(rth, p.get("or_minutes", 15))
    width = orh - orl
    if not (s.adr20 > 0) or not (p.get("min_width_adr", 0.10) <= width / s.adr20 <= p.get("max_width_adr", 0.60)):
        return []
    last = _minute_index(rth, p.get("last_entry", "11:30"))
    b5 = resample(rth, 5)
    out, done = [], set()
    for _, bar in b5.iterrows():
        li = int(bar["last_i"])
        if li < n_or - 1 or li + 1 >= last:
            continue
        for side, cond in ((1, bar["close"] > orh), (-1, bar["close"] < orl)):
            if cond and side not in done:
                stop = (orh + orl) / 2 if p.get("stop_mode", "mid") == "mid" else (orl if side > 0 else orh)
                t = simulate(Plan(side, li + 1, stop, setup="orb", **_plan_kwargs(p)), o, h, l, c, flat_i, costs)
                done.add(side)
                if t:
                    out.append(t)
    return out


# ----------------------------------------------------------------------------- 2. ORX
def orx(s: Session, p: dict, costs: Costs, flat_i: int) -> list[dict]:
    """Failed opening-range breakout. After the OR, a 5-min close beyond the OR starts a breakout; if a
    5-min close returns inside the OR within `fail_bars` 5-min bars, fade it at the next 1-min open.
    Stop = the breakout's extreme +/- buffer_ticks. Runner target = opposite OR side (target_mode 'or')
    or target_r. Entries until `last_entry`. At most one trade per direction."""
    rth = s.rth
    o, h, l, c, _ = _arrays(rth)
    orh, orl, n_or = opening_range(rth, p.get("or_minutes", 15))
    if not (s.adr20 > 0) or (orh - orl) / s.adr20 > p.get("max_width_adr", 0.60):
        return []
    last = _minute_index(rth, p.get("last_entry", "12:00"))
    b5 = resample(rth, 5).reset_index(drop=True)
    buf = p.get("buffer_ticks", 2) * costs.tick
    out, done = [], set()
    state = None   # (direction of breakout, bar number, extreme)
    for k, bar in b5.iterrows():
        li = int(bar["last_i"])
        if li < n_or - 1:
            continue
        if li + 1 >= last:
            break
        if state is not None:
            d, k0, ext = state
            ext = max(ext, bar["high"]) if d > 0 else min(ext, bar["low"])
            state = (d, k0, ext)
            back_inside = orl < bar["close"] < orh
            if back_inside and (-d) not in done:
                side = -d
                stop = ext + buf if side < 0 else ext - buf
                tp = orl if side < 0 else orh
                kw = _plan_kwargs(p)
                t = simulate(Plan(side, li + 1, stop, setup="orx",
                                  target_price=tp if p.get("target_mode", "or") == "or" else None, **kw),
                             o, h, l, c, flat_i, costs)
                done.add(side)
                state = None
                if t:
                    out.append(t)
                continue
            if k - k0 >= p.get("fail_bars", 3):
                state = None
        if state is None:
            if bar["close"] > orh:
                state = (1, k, bar["high"])
            elif bar["close"] < orl:
                state = (-1, k, bar["low"])
    return out


# ----------------------------------------------------------------------------- 3/4. CTTD and rev-CTTD
def cttd(s: Session, p: dict, costs: Costs, flat_i: int, reverse: bool = False) -> list[dict]:
    """Candlestick-tracking trend decision (Lin et al., 2023) on `bar_minutes` bars, one contract.
    Flat: an up bar closing above H -> leg up (H=close, L=low); a down bar closing below L -> leg down.
    In an up-leg: close > H trails (SecH=H, H=close); close < L reverses; close < SecH exits; else
    H=high, SecH=close. Mirror for down-legs. CTTD trades with the leg; rev-CTTD takes the other side.
    Orders fill at the next 1-min open. No entries before `start` or after `last_entry`; flat at flat time.
    A catastrophe stop at hard_stop_pts protects every position."""
    rth = s.rth
    o, h, l, c, _ = _arrays(rth)
    bars = resample(rth, p.get("bar_minutes", 3))
    start_i = _minute_index(rth, p.get("start", "09:45"))
    last_i = _minute_index(rth, p.get("last_entry", "15:30"))
    hard = p.get("hard_stop_pts", 60.0)
    name = "rcttd" if reverse else "cttd"
    m = -1 if reverse else 1
    H = L = secH = secL = None
    leg = 0                 # +1 up-leg, -1 down-leg, 0 flat
    pos = None              # (side, entry_i, ref_extreme)
    out = []

    def close_pos(at_i):
        nonlocal pos
        if pos is not None:
            side, ei, ref = pos
            if at_i > ei:
                out.append(simulate_flip_trade(side, ei, at_i, ref, o, h, l, c, costs, hard, name, flat_i))
            pos = None

    def open_pos(side, at_i):
        nonlocal pos
        if at_i < last_i and at_i < flat_i:
            pos = (side, at_i, L if side > 0 else H)   # reference extreme on the losing side

    for _, b in bars.iterrows():
        li = int(b["last_i"])
        nxt = li + 1
        if li + 1 < start_i:
            H, L = b["high"], b["low"]          # observation window: track the latest bar only
            continue
        if nxt >= flat_i:
            break
        cp, hp, lp, op = b["close"], b["high"], b["low"], b["open"]
        up, down = cp > op, cp < op
        if H is None:
            H, L = hp, lp
        if leg == 0:
            if up and cp > H:
                leg, H, L, secH = 1, cp, lp, None
                open_pos(m * 1, nxt)
            elif down and cp < L:
                leg, H, L, secL = -1, hp, cp, None
                open_pos(m * -1, nxt)
        elif leg == 1:
            if cp > H:                                   # a.1 continue: trail
                secH, H = H, cp
            elif cp < L:                                 # a.2 reversal
                close_pos(nxt)
                leg, H, L, secL = -1, hp, cp, None
                open_pos(m * -1, nxt)
            elif secH is not None and cp < secH:         # a.3 take profit / exit
                close_pos(nxt)
                leg, H, L = 0, hp, lp
            else:
                H, secH = hp, cp
        else:
            if cp < L:
                secL, L = L, cp
            elif cp > H:
                close_pos(nxt)
                leg, H, L, secH = 1, cp, lp, None
                open_pos(m * 1, nxt)
            elif secL is not None and cp > secL:
                close_pos(nxt)
                leg, H, L = 0, hp, lp
            else:
                L, secL = lp, cp
    close_pos(flat_i)
    return out


def rcttd(s, p, costs, flat_i):
    return cttd(s, p, costs, flat_i, reverse=True)


# ----------------------------------------------------------------------------- VWAP helpers
def vwap_bands(rth: pd.DataFrame):
    tp = (rth["high"] + rth["low"] + rth["close"]) / 3
    v = rth["volume"].replace(0, 1.0)
    cv = v.cumsum()
    vw = (tp * v).cumsum() / cv
    var = ((tp ** 2) * v).cumsum() / cv - vw ** 2
    return vw.to_numpy(), np.sqrt(np.maximum(var.to_numpy(), 0.0))


# ----------------------------------------------------------------------------- 5. VWAP pullback
def vwap_pullback(s: Session, p: dict, costs: Costs, flat_i: int) -> list[dict]:
    """Trend pullback to VWAP on 1-min bars between `start` and `last_entry`.
    Long when: the previous `trend_minutes` closes were all above VWAP; VWAP is higher than
    `trend_minutes` ago; this bar's low reaches VWAP + touch_pts or lower; it closes above VWAP and above
    its open. Stop = bar low - buffer_ticks. Short = mirror. At most `max_per_side` per side, with a
    `cooldown` (minutes) between entries."""
    rth = s.rth
    o, h, l, c, _ = _arrays(rth)
    vw, _ = vwap_bands(rth)
    n = p.get("trend_minutes", 30)
    i0, i1 = _minute_index(rth, p.get("start", "10:00")), _minute_index(rth, p.get("last_entry", "14:30"))
    touch, buf = p.get("touch_pts", 2.0), p.get("buffer_ticks", 2) * costs.tick
    out, count, last_entry = [], {1: 0, -1: 0}, -10**9
    for j in range(max(i0, n), min(i1, len(c) - 1)):
        if j - last_entry < p.get("cooldown", 15):
            continue
        prev_c, prev_v = c[j - n:j], vw[j - n:j]
        if count[1] < p.get("max_per_side", 2) and (prev_c > prev_v).all() and vw[j] > vw[j - n] \
                and l[j] <= vw[j] + touch and c[j] > vw[j] and c[j] > o[j]:
            t = simulate(Plan(1, j + 1, l[j] - buf, setup="vwap_pullback", **_plan_kwargs(p)), o, h, l, c, flat_i, costs)
            if t:
                out.append(t)
                count[1] += 1
                last_entry = j
            continue
        if count[-1] < p.get("max_per_side", 2) and (prev_c < prev_v).all() and vw[j] < vw[j - n] \
                and h[j] >= vw[j] - touch and c[j] < vw[j] and c[j] < o[j]:
            t = simulate(Plan(-1, j + 1, h[j] + buf, setup="vwap_pullback", **_plan_kwargs(p)), o, h, l, c, flat_i, costs)
            if t:
                out.append(t)
                count[-1] += 1
                last_entry = j
    return out


# ----------------------------------------------------------------------------- 6. VWAP band fade
def vwap_fade(s: Session, p: dict, costs: Costs, flat_i: int) -> list[dict]:
    """Fade an extension beyond the VWAP band between `start` and `last_entry` (1-min bars).
    Arm when a close is beyond VWAP +/- k*sigma; enter when a later close comes back inside the band with a
    candle in the fade direction. Stop = extreme since arming +/- buffer_ticks. Runner target = VWAP at the
    signal (if it is beyond PT1) else target_r. At most `max_per_side` per side."""
    rth = s.rth
    o, h, l, c, _ = _arrays(rth)
    vw, sd = vwap_bands(rth)
    k = p.get("band_k", 2.0)
    i0, i1 = _minute_index(rth, p.get("start", "10:00")), _minute_index(rth, p.get("last_entry", "15:00"))
    buf = p.get("buffer_ticks", 2) * costs.tick
    out, count = [], {1: 0, -1: 0}
    armed = 0
    ext = None
    for j in range(i0, min(i1, len(c) - 1)):
        up, dn = vw[j] + k * sd[j], vw[j] - k * sd[j]
        if armed == 0:
            if c[j] > up and sd[j] > 0:
                armed, ext = 1, h[j]
            elif c[j] < dn and sd[j] > 0:
                armed, ext = -1, l[j]
            continue
        ext = max(ext, h[j]) if armed > 0 else min(ext, l[j])
        if armed > 0 and c[j] < up and c[j] < o[j] and count[-1] < p.get("max_per_side", 2):
            t = simulate(Plan(-1, j + 1, ext + buf, setup="vwap_fade", target_price=vw[j], **_plan_kwargs(p)),
                         o, h, l, c, flat_i, costs)
            if t:
                out.append(t)
                count[-1] += 1
            armed = 0
        elif armed < 0 and c[j] > dn and c[j] > o[j] and count[1] < p.get("max_per_side", 2):
            t = simulate(Plan(1, j + 1, ext - buf, setup="vwap_fade", target_price=vw[j], **_plan_kwargs(p)),
                         o, h, l, c, flat_i, costs)
            if t:
                out.append(t)
                count[1] += 1
            armed = 0
    return out


SETUPS = {"orb": orb, "orx": orx, "cttd": cttd, "rcttd": rcttd, "vwap_pullback": vwap_pullback, "vwap_fade": vwap_fade}

"""Single-trade simulation on 1-minute bars.

Fill model (pessimistic by design):
  * entries are market orders at the OPEN of the 1-min bar after the signal, plus `slip_ticks`.
  * stops are stop-market: filled at the stop, or at the bar open if the bar gaps through, minus `slip_ticks`.
  * profit targets are limits: filled only if price trades THROUGH the level by `through_ticks`.
  * if a bar touches both the stop and a target, the stop is assumed to fill first.
  * open trades are closed at the open of the `flat_time` bar (default 15:55).
Costs: commission per contract per side (USD) converted to points, applied to every fill.
Results are in R, where 1R = initial stop distance in points.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Plan:
    side: int                  # +1 long, -1 short
    entry_i: int               # 1-min bar index whose open is the entry
    stop: float                # initial stop price (before min/max adjustment)
    pt1_r: float = 1.0         # first target in R (0 disables)
    pt1_frac: float = 0.5      # share closed at PT1
    target_r: float = 2.0      # runner target in R (0 = no fixed target)
    target_price: float | None = None   # explicit runner target price (overrides target_r)
    be_after_pt1: bool = True
    setup: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Costs:
    tick: float = 0.25
    point_value: float = 2.0
    commission_usd: float = 1.0      # per contract per side
    slip_ticks: float = 1.0          # on market and stop fills
    through_ticks: float = 1.0       # limit fills need price to trade through by this much
    min_stop_pts: float = 10.0
    max_stop_pts: float = 60.0

    @property
    def comm_pts(self) -> float:
        return self.commission_usd / self.point_value


def simulate(plan: Plan, o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, flat_i: int, costs: Costs):
    """Returns a dict with net_R, exit info and the per-minute open R path, or None if the stop is too wide."""
    s, k = plan.side, plan.entry_i
    if k >= flat_i or k >= len(o):
        return None
    entry = o[k] + s * costs.slip_ticks * costs.tick
    stop = plan.stop
    risk = (entry - stop) * s
    if risk < costs.min_stop_pts:
        stop = entry - s * costs.min_stop_pts
        risk = costs.min_stop_pts
    if risk > costs.max_stop_pts:
        return None
    stop_init = stop
    pt1 = entry + s * plan.pt1_r * risk if plan.pt1_r > 0 else None
    if plan.target_price is not None and (plan.target_price - entry) * s > 0:
        tgt = plan.target_price
    elif plan.target_r > 0:
        tgt = entry + s * plan.target_r * risk
    else:
        tgt = None
    thr = costs.through_ticks * costs.tick
    slip = costs.slip_ticks * costs.tick
    open_frac, realized, pt1_hit = 1.0, 0.0, False
    fills = 1
    path = []
    exit_reason, exit_i, exit_px = "flat", flat_i, None
    for j in range(k, min(flat_i, len(o))):
        # stop first (pessimistic)
        if (l[j] <= stop) if s > 0 else (h[j] >= stop):
            px = (min(o[j], stop) if s > 0 else max(o[j], stop)) - s * slip
            if j == k:   # stop inside entry bar: cannot be better than stop
                px = stop - s * slip
            realized += open_frac * (px - entry) * s
            fills += 1
            exit_reason, exit_i, exit_px, open_frac = ("be_stop" if pt1_hit and plan.be_after_pt1 else "stop"), j, px, 0.0
            path.append(realized)
            break
        if pt1 is not None and not pt1_hit and ((h[j] >= pt1 + thr) if s > 0 else (l[j] <= pt1 - thr)):
            realized += plan.pt1_frac * (pt1 - entry) * s
            open_frac -= plan.pt1_frac
            fills += 1
            pt1_hit = True
            if plan.be_after_pt1:
                stop = entry
        if tgt is not None and open_frac > 0 and ((h[j] >= tgt + thr) if s > 0 else (l[j] <= tgt - thr)):
            realized += open_frac * (tgt - entry) * s
            fills += 1
            exit_reason, exit_i, exit_px, open_frac = "target", j, tgt, 0.0
            path.append(realized)
            break
        path.append(realized + open_frac * (c[j] - entry) * s)
    if open_frac > 0:
        j = min(flat_i, len(o) - 1)
        px = o[j] - s * slip
        realized += open_frac * (px - entry) * s
        fills += 1
        exit_i, exit_px = j, px
    cost_pts = fills * costs.comm_pts
    net_pts = realized - cost_pts
    path = np.asarray(path, dtype=float) - costs.comm_pts   # entry commission paid up front
    return {"setup": plan.setup, "side": s, "entry_i": k, "exit_i": exit_i, "entry": entry, "stop0": stop_init,
            "risk_pts": risk, "exit": exit_px, "exit_reason": exit_reason, "pt1_hit": pt1_hit,
            "gross_R": realized / risk, "net_R": net_pts / risk, "path_R": path / risk, **plan.meta}


def simulate_flip_trade(side: int, entry_i: int, exit_i: int, ref_stop: float, o, h, l, c, costs: Costs,
                        hard_stop_pts: float, setup: str, flat_i: int):
    """For state-machine setups (CTTD): exit decided by the state machine at exit_i's open, plus an intrabar
    catastrophe stop at hard_stop_pts. R = |entry - reference extreme| clipped to [min_stop, max_stop]."""
    entry = o[entry_i] + side * costs.slip_ticks * costs.tick
    risk = min(max(abs(entry - ref_stop), costs.min_stop_pts), costs.max_stop_pts)
    hard = entry - side * hard_stop_pts
    slip = costs.slip_ticks * costs.tick
    path, reason, px, end = [], "signal", None, min(exit_i, flat_i)
    for j in range(entry_i, end):
        if (l[j] <= hard) if side > 0 else (h[j] >= hard):
            px = (min(o[j], hard) if side > 0 else max(o[j], hard)) - side * slip
            reason, end = "hard_stop", j
            path.append((px - entry) * side)
            break
        path.append((c[j] - entry) * side)
    if px is None:
        j = min(end, len(o) - 1)
        px = o[j] - side * slip
        reason = "flat" if end == flat_i and exit_i >= flat_i else "signal"
    net = (px - entry) * side - 2 * costs.comm_pts
    p = (np.asarray(path, dtype=float) - costs.comm_pts) / risk
    return {"setup": setup, "side": side, "entry_i": entry_i, "exit_i": end, "entry": entry, "stop0": ref_stop,
            "risk_pts": risk, "exit": px, "exit_reason": reason, "pt1_hit": False,
            "gross_R": (px - entry) * side / risk, "net_R": net / risk, "path_R": p}

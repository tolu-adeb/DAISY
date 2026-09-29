"""Trade-idea lifecycle: a pure, deterministic state machine.

    pending ──(price reaches entry trigger & grade ok)──► active ──► closed
       │  ├─ approaching (heads-up, once)                   │  ├─ target_hit (partial; stop → breakeven / T1)
       │  ├─ entry_blocked (grade D; re-checked later)      │  ├─ stop_hit / breakeven stop / trailing stop
       │  ├─ invalidated (stop hit before entry)            │  ├─ time_exit (max hold reached)
       │  ├─ missed (ran to T1 without an entry)            │  └─ closed by the source ("closing here")
       │  └─ expired (entry never reached in time)          │
       └─ cancelled (by the source or the user) ◄───────────┘

``step(idea, obs)`` consumes one price observation - a live quote (high = low = price) or a
daily bar (open/high/low/close) during catch-up - and returns the events it produced.
Within one bar the order is conservative: for an open position the stop is checked before
targets (we can't know the intrabar sequence, so assume the worse outcome); gaps through
a level fill at the open, not at the level.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

OPEN_STATES = {"pending", "active"}
FINAL_STATES = {"closed", "invalidated", "missed", "expired", "cancelled", "rejected"}


@dataclass
class Idea:
    symbol: str
    direction: str                         # long | short
    entry_type: str                        # zone | market | breakout_above | breakdown_below | limit_below | limit_above
    entry_low: float | None
    entry_high: float | None
    stop: float | None
    targets: list[float]
    id: int | None = None
    status: str = "pending"
    stop_initial: float | None = None
    stop_basis: str = "touch"
    instrument: str = "stock"
    timeframe: str = "swing"
    source: str = "manual"
    channel_id: str | None = None
    channel_name: str | None = None
    author: str | None = None
    message_id: str | None = None
    raw: str = ""
    parse_confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    max_hold_until: float | None = None
    # execution (paper)
    entry_price: float | None = None
    entry_at: float | None = None
    shares: float = 0.0
    remaining: float = 1.0                 # fraction of the position still open
    targets_hit: list[int] = field(default_factory=list)
    realized_r: float = 0.0
    realized_pct: float = 0.0
    realized_pnl: float = 0.0
    exit_value: float = 0.0                # sum(fraction * exit price) for the blended exit
    closed_at: float | None = None
    close_reason: str | None = None
    grade: str | None = None
    grade_score: float | None = None
    grade_reasons: list[str] = field(default_factory=list)
    last_price: float | None = None
    last_checked_at: float | None = None
    mfe_pct: float = 0.0                   # max favourable excursion since entry
    mae_pct: float = 0.0                   # max adverse excursion since entry
    flags: dict = field(default_factory=dict)
    relay_ref: str | None = None           # our first relay message id (for threaded replies)
    soft_stop: float | None = None         # inner edge of a stop range: a warning, not an exit
    meta: dict = field(default_factory=dict)       # source thesis, scores, classification, earnings date
    tranches: list = field(default_factory=list)   # scale-in plan: [{level, frac, fill, at, cancelled}]
    filled: float = 0.0                    # fraction of the planned size bought so far

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Idea":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    # ---- geometry helpers
    @property
    def long(self) -> bool:
        return self.direction == "long"

    @property
    def ref_entry(self) -> float | None:
        """The worst edge of the entry zone (used for R:R planning)."""
        if self.entry_high is None:
            return None
        return self.entry_high if self.long else self.entry_low

    @property
    def risk_per_share(self) -> float | None:
        e = self.entry_price if self.entry_price is not None else self.ref_entry
        s = self.stop_initial if self.stop_initial is not None else self.stop
        if e is None or s is None:
            return None
        return abs(e - s) or None

    def rr(self, target: float, entry: float | None = None) -> float | None:
        e = entry if entry is not None else (self.entry_price if self.entry_price is not None else self.ref_entry)
        r = abs(e - self.stop) if (e is not None and self.stop is not None) else None
        if not r:
            return None
        return (target - e) / r if self.long else (e - target) / r

    def distance_to_entry_pct(self, price: float) -> float | None:
        """Signed % the price must move to reach the entry trigger (0 = inside)."""
        if self.entry_type == "market" or self.entry_low is None or not price:
            return 0.0
        lo, hi = self.entry_low, self.entry_high
        et = self.entry_type
        if et in ("zone",):
            if lo <= price <= hi:
                return 0.0
            edge = hi if price > hi else lo
        elif et in ("breakout_above", "limit_above"):
            if price >= lo:
                return 0.0
            edge = lo
        else:                                   # breakdown_below, limit_below
            if price <= lo:
                return 0.0
            edge = lo
        return (edge / price - 1) * 100

    def open_pnl(self, price: float | None) -> float:
        if self.entry_price is None or not price or self.status != "active":
            return 0.0
        move = (price - self.entry_price) if self.long else (self.entry_price - price)
        return move * self.shares * self.remaining * self.flags.get("multiplier", 1.0)

    def open_risk(self) -> float:
        """$ lost if the current stop is hit (negative = the stop locks in profit)."""
        if self.status != "active" or self.stop is None or self.entry_price is None:
            return 0.0
        move = (self.entry_price - self.stop) if self.long else (self.stop - self.entry_price)
        return move * self.shares * self.remaining * self.flags.get("multiplier", 1.0)

    def open_r(self, price: float) -> float:
        if self.entry_price is None or not self.risk_per_share:
            return 0.0
        move = (price - self.entry_price) if self.long else (self.entry_price - price)
        return move / self.risk_per_share * self.remaining

    def total_r(self, price: float | None = None) -> float:
        return self.realized_r + (self.open_r(price) if price and self.status == "active" else 0.0)


@dataclass
class Obs:
    ts: float
    price: float                           # last / close
    high: float
    low: float
    open: float | None = None
    bar: bool = False                      # True for daily-bar catch-up


def plan_tranches(idea: Idea, split: float = 0.5, min_width_pct: float = 0.4) -> list:
    """Scale-in plan for a zone: ``split`` at the near edge, the rest at the zone midpoint.
    Narrow zones, breakouts and market entries use one tranche at the trigger."""
    lo, hi = idea.entry_low, idea.entry_high
    if (idea.entry_type == "zone" and lo is not None and hi is not None and hi > lo
            and (hi - lo) / hi * 100 >= min_width_pct and 0 < split < 1):
        near = hi if idea.long else lo
        idea.tranches = [{"level": near, "frac": split, "fill": None, "at": None, "cancelled": False},
                         {"level": round((lo + hi) / 2, 4), "frac": round(1 - split, 6), "fill": None, "at": None,
                          "cancelled": False}]
    else:
        idea.tranches = [{"level": None, "frac": 1.0, "fill": None, "at": None, "cancelled": False}]
    return idea.tranches


def _level_fill(idea: Idea, level: float, o: "Obs", prev: float | None = None) -> float | None:
    """Resting limit at ``level``.  Daily bars: the open if it gapped through, else the level.  Live
    quotes: the level when price crossed it since the last quote, the current price if it was already
    through the level when the order was placed."""
    op = o.open if o.open is not None else o.price
    if idea.long:
        if o.low > level:
            return None
        if o.bar:
            return op if op <= level else level
        return level if (prev is not None and prev > level) else o.price
    if o.high < level:
        return None
    if o.bar:
        return op if op >= level else level
    return level if (prev is not None and prev < level) else o.price


def _fill_tranches(idea: Idea, o: "Obs", first_fill: float | None = None, prev: float | None = None) -> list[dict]:
    """Fill every open tranche this observation reaches.  Updates entry_price (average), filled, remaining."""
    if not idea.tranches:
        idea.tranches = [{"level": None, "frac": 1.0, "fill": None, "at": None, "cancelled": False}]
    new = []
    for i, t in enumerate(idea.tranches):
        if t["fill"] is not None or t.get("cancelled"):
            continue
        px = (first_fill if (i == 0 and first_fill is not None) else
              trigger_fill(idea, o) if t["level"] is None else _level_fill(idea, t["level"], o, prev))
        if px is None:
            continue
        t["fill"], t["at"] = px, o.ts
        new.append({"tranche": i, "price": px, "frac": t["frac"]})
    if new:
        done = [t for t in idea.tranches if t["fill"] is not None]
        idea.filled = round(sum(t["frac"] for t in done), 10)
        idea.entry_price = sum(t["fill"] * t["frac"] for t in done) / idea.filled
        idea.remaining = round(idea.remaining + sum(x["frac"] for x in new), 10) if idea.status == "active" \
            else idea.filled
    return new


def open_tranches(idea: Idea) -> list[dict]:
    return [t for t in idea.tranches if t["fill"] is None and not t.get("cancelled")]


def _event(kind: str, price: float, **data) -> dict:
    return {"type": kind, "price": price, **data}


def trigger_fill(idea: Idea, o: Obs) -> float | None:
    """Price at which the entry would fill on this observation, or None."""
    lo, hi, et = idea.entry_low, idea.entry_high, idea.entry_type
    op = o.open if o.open is not None else o.price
    if et == "market":
        return o.price
    if et == "zone":
        if idea.long:
            if o.low <= hi:                                 # touched the top of the zone (or below)
                return op if op <= hi else hi               # opened inside/below the zone -> open, else the zone top
        elif o.high >= lo:                                  # short: touched the bottom of the zone (or above)
            return op if op >= lo else lo
        return None
    if et in ("breakout_above", "limit_above"):
        return max(lo, op) if o.high >= lo else None
    if et in ("breakdown_below", "limit_below"):
        if o.low <= lo:
            return min(lo, op)
        return None
    return None


def step(idea: Idea, o: Obs, *, approach_pct: float = 1.5, move_stop_to_be: bool = True,
         allow_entry: bool = True, near_pct: float | None = None) -> list[dict]:
    """Advance the idea with one observation.  Mutates ``idea`` and returns emitted events.

    ``allow_entry=False`` means the grade gate is closed: an entry trigger emits
    ``entry_blocked`` instead of filling (the tracker re-grades later).
    """
    ev: list[dict] = []
    prev = idea.last_price
    idea.last_price, idea.last_checked_at = o.price, o.ts
    if idea.status not in OPEN_STATES:
        return ev

    if idea.status == "pending":
        if idea.expires_at and o.ts > idea.expires_at:
            idea.status, idea.closed_at, idea.close_reason = "expired", o.ts, "entry never reached"
            return [_event("expired", o.price)]
        added = set(idea.flags.get("added_targets") or [])      # our own scale-out levels don't make it "missed"
        t1 = next((t for t in idea.targets if t not in added), None)
        first = idea.tranches[0] if idea.tranches else None
        fill = (_level_fill(idea, first["level"], o, prev) if (first and first.get("level") is not None)
                else trigger_fill(idea, o))
        stop_hit_pre = idea.stop is not None and ((o.low <= idea.stop) if idea.long else (o.high >= idea.stop))
        if fill is not None and idea.stop is not None and ((fill <= idea.stop) if idea.long else (fill >= idea.stop)):
            idea.status, idea.closed_at, idea.close_reason = "invalidated", o.ts, "gapped through the stop before entry"
            return [_event("invalidated", o.price, reason=idea.close_reason)]
        if fill is None:
            if stop_hit_pre:
                idea.status, idea.closed_at, idea.close_reason = "invalidated", o.ts, "stop level traded before entry"
                return [_event("invalidated", o.price, reason=idea.close_reason)]
            if t1 is not None and ((o.high >= t1) if idea.long else (o.low <= t1)) and idea.entry_type != "market":
                idea.status, idea.closed_at, idea.close_reason = "missed", o.ts, "reached target 1 without an entry"
                return [_event("missed", o.price, reason=idea.close_reason)]
            d = idea.distance_to_entry_pct(o.price)
            if d is not None and abs(d) <= approach_pct and not idea.flags.get("approach_alerted"):
                idea.flags["approach_alerted"] = True
                ev.append(_event("approaching", o.price, distance_pct=d))
            return ev
        if t1 is not None and ((fill >= t1) if idea.long else (fill <= t1)):
            idea.status, idea.closed_at, idea.close_reason = "missed", o.ts, "price was already beyond target 1 at the trigger"
            return [_event("missed", o.price, reason=idea.close_reason)]
        if not allow_entry:
            if not idea.flags.get("blocked_at"):
                idea.flags["blocked_at"] = o.ts
                ev.append(_event("entry_blocked", fill))
            return ev
        fills = _fill_tranches(idea, o, first_fill=fill, prev=prev)
        idea.status, idea.entry_at = "active", o.ts
        idea.flags.pop("blocked_at", None)
        if idea.stop_initial is None:
            idea.stop_initial = idea.stop
        ev.append(_event("entry", idea.entry_price, zone=[idea.entry_low, idea.entry_high], fills=fills,
                         fraction=idea.filled, pending_tranches=[t["level"] for t in open_tranches(idea)]))
        # a daily bar that fills can also hit the stop/targets later that same bar: fall through

    # ---- active
    if idea.filled <= 0:                                   # ideas entered before scale-in plans existed
        idea.filled = 1.0
    if open_tranches(idea) and idea.entry_at != o.ts:     # scale in on the way down (long) / up (short)
        fills = _fill_tranches(idea, o, prev=prev)
        if fills:
            ev.append(_event("scale_in", fills[-1]["price"], fills=fills, fraction=idea.filled, avg=idea.entry_price))
    e = idea.entry_price
    fav = ((o.high / e - 1) if idea.long else (1 - o.low / e)) * 100
    adv = ((o.low / e - 1) if idea.long else (1 - o.high / e)) * 100
    idea.mfe_pct, idea.mae_pct = max(idea.mfe_pct, fav), min(idea.mae_pct, adv)
    op = o.open if (o.open is not None and o.bar and idea.entry_at != o.ts) else o.price

    stop = idea.stop
    if stop is not None:
        use = o.price if (idea.stop_basis == "close" and o.bar) else (o.low if idea.long else o.high)
        if idea.stop_basis == "close" and not o.bar:
            use = None                                      # close-basis stops are only judged on bar closes
        hit = use is not None and ((use <= stop) if idea.long else (use >= stop))
        if hit:
            px = (min(stop, op) if idea.long else max(stop, op)) if idea.stop_basis == "touch" else o.price
            kind = "breakeven_stop" if abs(stop - e) < 1e-9 else ("trailing_stop" if idea.targets_hit else "stop_hit")
            ev.append(_exit(idea, px, idea.remaining, o.ts, kind))
            idea.status, idea.closed_at, idea.close_reason = "closed", o.ts, kind.replace("_", " ")
            return ev

    n = len(idea.targets)
    for i, t in enumerate(idea.targets):
        if i in idea.targets_hit:
            continue
        reached = (o.high >= t) if idea.long else (o.low <= t)
        if not reached:
            break
        # resting limit: fills at the target, or better if a daily bar gapped through it at the open
        px = (max(t, op) if idea.long else min(t, op)) if o.bar else t
        frac = idea.remaining if i == n - 1 else min(idea.remaining, idea.filled / n)
        idea.targets_hit.append(i)
        cancelled = []
        for tr in open_tranches(idea):                    # no adding once profits are being taken
            tr["cancelled"] = True
            cancelled.append(tr["level"])
        ev.append(_exit(idea, px, frac, o.ts, "target_hit", target_index=i, target=t, cancelled_tranches=cancelled))
        if idea.remaining <= 1e-9:
            idea.status, idea.closed_at, idea.close_reason = "closed", o.ts, f"final target {i + 1} hit"
            return ev
        new_stop = None
        if i == 0 and move_stop_to_be:
            new_stop = e
        elif i >= 1:
            new_stop = idea.targets[i - 1]
        if new_stop is not None and (idea.stop is None or (new_stop > idea.stop if idea.long else new_stop < idea.stop)):
            old, idea.stop = idea.stop, new_stop
            ev.append(_event("stop_moved", o.price, old=old, new=new_stop,
                             reason="breakeven after target 1" if i == 0 else f"trailing to target {i}"))

    if idea.max_hold_until and o.ts > idea.max_hold_until and idea.status == "active":
        ev.append(_exit(idea, o.price, idea.remaining, o.ts, "time_exit"))
        idea.status, idea.closed_at, idea.close_reason = "closed", o.ts, "maximum holding period reached"
        return ev
    if idea.status == "active":
        ev += _heads_up(idea, o, near_pct)
    return ev


def _heads_up(idea: Idea, o: Obs, near_pct: float | None) -> list[dict]:
    """One-off warnings while a trade is open: stop range entered, stop close, next target close."""
    ev = []
    px = o.price
    ss = idea.soft_stop
    if ss is not None and idea.stop is not None and not idea.flags.get("soft_stop_alerted") and \
            ((o.low <= ss) if idea.long else (o.high >= ss)) and ((ss > idea.stop) if idea.long else (ss < idea.stop)):
        idea.flags["soft_stop_alerted"] = True
        ev.append(_event("soft_stop", px, soft=ss, hard=idea.stop))
    if not near_pct or not px:
        return ev
    if idea.stop is not None:
        d = ((px - idea.stop) if idea.long else (idea.stop - px)) / px * 100
        if 0 < d <= near_pct and not idea.flags.get("stop_near"):
            idea.flags["stop_near"] = True
            ev.append(_event("stop_near", px, distance_pct=d, stop=idea.stop))
        elif d > 2 * near_pct:
            idea.flags.pop("stop_near", None)
    nxt = next(((j, t) for j, t in enumerate(idea.targets) if j not in idea.targets_hit), None)
    if nxt:
        j, t = nxt
        d = ((t - px) if idea.long else (px - t)) / px * 100
        if 0 < d <= near_pct and idea.flags.get("target_near") != j:
            idea.flags["target_near"] = j
            ev.append(_event("target_near", px, distance_pct=d, target=t, target_index=j))
    return ev


def _exit(idea: Idea, px: float, frac: float, ts: float, kind: str, **data) -> dict:
    frac = max(0.0, min(frac, idea.remaining))
    e = idea.entry_price
    r = idea.risk_per_share
    move = (px - e) if idea.long else (e - px)
    idea.realized_r += (move / r * frac) if r else 0.0
    idea.realized_pct += move / e * 100 * frac
    idea.realized_pnl += move * idea.shares * frac * idea.flags.get("multiplier", 1.0)   # $ per point for futures
    idea.exit_value += px * frac
    idea.remaining = round(idea.remaining - frac, 10)
    return _event(kind, px, fraction=frac, r=(move / r) if r else None, pct=move / e * 100, **data)


def manual_exit(idea: Idea, price: float, ts: float, fraction: float | None = None, reason: str = "closed by source") -> dict:
    frac = idea.remaining if fraction is None else min(fraction, idea.remaining)
    ev = _exit(idea, price, frac, ts, "exit" if fraction is None or idea.remaining - frac <= 1e-9 else "trim", reason=reason)
    if idea.remaining <= 1e-9:
        idea.status, idea.closed_at, idea.close_reason = "closed", ts, reason
    return ev


def summary_stats(ideas: list[Idea]) -> dict:
    rejected = sum(i.status == "rejected" for i in ideas)
    ideas = [i for i in ideas if i.status != "rejected"]          # never tracked: not part of the track record
    closed = [i for i in ideas if i.status == "closed" and i.entry_price is not None]
    wins = [i for i in closed if i.realized_r > 0.05]
    losses = [i for i in closed if i.realized_r < -0.05]
    rs = [i.realized_r for i in closed]
    gross_w = sum(r for r in rs if r > 0)
    gross_l = -sum(r for r in rs if r < 0)
    by = {}
    for i in ideas:
        key = i.author or i.channel_name or i.source
        b = by.setdefault(key, {"ideas": 0, "triggered": 0, "closed": 0, "wins": 0, "total_r": 0.0})
        b["ideas"] += 1
        b["triggered"] += i.entry_price is not None
        if i.status == "closed" and i.entry_price is not None:
            b["closed"] += 1
            b["wins"] += i.realized_r > 0.05
            b["total_r"] += i.realized_r
    for b in by.values():
        b["win_rate"] = b["wins"] / b["closed"] if b["closed"] else None
        b["avg_r"] = b["total_r"] / b["closed"] if b["closed"] else None
    return {
        "ideas": len(ideas), "pending": sum(i.status == "pending" for i in ideas),
        "active": sum(i.status == "active" for i in ideas), "closed": len(closed),
        "never_filled": sum(i.status in ("expired", "missed", "invalidated") for i in ideas),
        "win_rate": len(wins) / len(closed) if closed else None, "wins": len(wins), "losses": len(losses),
        "avg_r": sum(rs) / len(rs) if rs else None, "total_r": sum(rs),
        "profit_factor": (gross_w / gross_l) if gross_l > 0 else None,
        "expectancy_r": sum(rs) / len(rs) if rs else None,
        "realized_pnl": sum(i.realized_pnl for i in ideas), "rejected": rejected, "by_source": by,
    }

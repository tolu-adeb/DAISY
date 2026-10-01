"""MNQ opening-session strategy: one engine for live signals and backtests.

Feed it bars (1-minute ideally, 5-minute works) for one RTH session at a time:

    strat.start_day(ctx, warmup_bars)   -> [brief]
    strat.on_bar(bar)                   -> [events]   (call for every bar 09:30-16:00 ET)
    strat.end_day()                     -> [events]

Every event is a plain dict with a ``type`` (brief, idea, idea_cancel, signal, heads_up, t1, scaleout,
runner, final, stopped, breakeven, closed, done, session_closed, day_summary) and the facts and reasons
the Discord messages need.  Nothing here touches the network, so a backtest runs exactly the code the
live bot runs.

Setups (all in the 09:35-10:30 window by default - the evidence for that is in docs/14):

* ``orb``   - opening-range breakout and retest.  A 5-minute close beyond the 15-minute opening range
              with a real body arms a trade idea; the signal validates when price comes back into the
              zone at the range edge and a bar closes back in the breakout direction.
* ``sweep`` - liquidity sweep and reclaim.  Price runs a key level (yesterday's high/low, overnight
              high/low, opening-range high/low) by a little - not a lot - then a 5-minute candle closes
              back across it with displacement.  Stop goes beyond the sweep extreme.
* ``vwap``  - trend pullback.  On a one-sided day (price held one side of VWAP, efficient move), the
              first clean pullback to VWAP that closes back in the trend direction.

Risk and management: stop sized from structure and bounded by points; Target 1 at 1R banks half and
moves the stop to break-even (or later / softer, see ``be_mode``); the runner trails by ATR and exits at
the final target (next liquidity level, at least 1.8R) or the flat time.  Day rules: max 2 trades, stop
after the first loss, no new entries after the window, a daily loss cap in R, news blackouts, half
size (or skip) on FOMC days.  One trade per zone - the same idea can't fire twice.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time, timedelta

from .context import DayContext
from .data import Bar


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _mins(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class AlphaParams:
    or_minutes: int = 15
    entry_start: str = "09:35"
    entry_end: str = "10:30"
    vwap_start: str = "09:50"
    flat_time: str = "15:55"
    max_trades: int = 2
    stop_after_loss: bool = True
    max_daily_loss_r: float = 1.5
    setups: tuple = ("orb", "sweep", "vwap")
    atr_len: int = 14
    disp_body_atr: float = 0.6
    zone_atr: float = 0.25
    stop_buffer_atr: float = 0.15
    stop_atr: float = 0.9
    min_risk_pts: float = 12.0
    max_risk_pts: float = 90.0
    sweep_min_atr: float = 0.12
    sweep_max_atr: float = 1.2
    sweep_window_min: int = 25
    t1_r: float = 1.0
    final_r: float = 2.5
    min_final_r: float = 1.8
    max_final_r: float = 4.0
    be_mode: str = "t1"                 # t1 | t1_close | lock
    scale_frac: float = 0.5
    trail_atr: float = 1.5
    min_score: float = 55.0
    idea_expiry_min: int = 45
    news_before_min: int = 5
    news_after_min: int = 10
    fomc_mode: str = "half"             # skip | half | normal
    chase_r: float = 0.35
    slippage_ticks: int = 1
    tick: float = 0.25

    @classmethod
    def from_dict(cls, d: dict | None) -> "AlphaParams":
        d = dict(d or {})
        names = {f.name for f in fields(cls)}
        if "setups" in d and isinstance(d["setups"], list):
            d["setups"] = tuple(d["setups"])
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        d = asdict(self)
        d["setups"] = list(self.setups)
        return d


@dataclass
class Idea:
    id: int
    setup: str
    side: int                     # +1 long / -1 short
    level_name: str
    level: float
    zone_lo: float
    zone_hi: float
    optimal: float
    stop: float
    created: datetime
    expires: datetime
    key: str
    reasons: list[str] = field(default_factory=list)
    extreme: float | None = None  # sweep extreme
    state: str = "armed"          # watch | armed | validated | cancelled | expired
    posted: bool = False


@dataclass
class Trade:
    id: int
    setup: str
    side: int
    entry: float
    stop0: float
    stop: float
    risk: float
    t1: float
    final: float
    final_name: str
    opened: datetime
    zone: tuple[float, float]
    optimal: float
    score: float
    regime: str
    reasons: list[str]
    key: str
    size: float = 1.0
    level_name: str = ""
    t1_hit: bool = False
    be_active: bool = False
    banked: float = 0.0           # points banked on the scaled-out fraction
    mfe: float = 0.0
    mae: float = 0.0
    heads_up: bool = False
    last_runner: datetime | None = None
    runner_step: int = 0
    exit: float | None = None
    exit_reason: str | None = None
    closed: datetime | None = None
    pts: float | None = None      # blended points per contract
    r: float | None = None
    from_idea: bool = False

    def open_pts(self, px: float) -> float:
        return (px - self.entry) * self.side

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("opened", "closed", "last_runner"):
            d[k] = d[k].isoformat() if d[k] else None
        d["side_label"] = "LONG" if self.side > 0 else "SHORT"
        return d


class AlphaStrategy:
    def __init__(self, params: AlphaParams | None = None, learner=None, symbol: str = "MNQ"):
        self.p = params or AlphaParams()
        self.learner = learner
        self.symbol = symbol
        self.ctx: DayContext | None = None
        self.history: list[Trade] = []

    # ================================================================== day lifecycle
    def start_day(self, ctx: DayContext, warmup: list[Bar] | None = None) -> list[dict]:
        p = self.p
        self.ctx = ctx
        self.ideas: list[Idea] = []
        self.trade: Trade | None = None
        self.trades: list[Trade] = []
        self.next_id = 1
        self.used_keys: set[str] = set()
        self.seen_keys: set[str] = set()          # one idea per zone per day (no re-firing the same setup)
        self.lost = False
        self.day_r = 0.0
        self.done_reason: str | None = None
        self.window_closed = False
        self.or_hi = self.or_lo = None
        self.or_done = False
        self.open_px = None
        self.prev_bar: Bar | None = None
        self.bars: list[Bar] = []
        self.five: Bar | None = None
        self.fives: list[Bar] = []
        self.atr5: float | None = None
        self._tr_seed: list[float] = []
        self.cum_pv = self.cum_v = 0.0
        self.cum_tp = 0.0
        self.vwap: float | None = None
        self.vwap_side: int = 0
        self.vwap_crosses = 0
        self.side_counts = {1: 0, -1: 0}
        self.path_len = 0.0
        self.vwap_hist: list[tuple[datetime, float]] = []
        self.flat_done = False
        self.size_mult = 1.0
        if ctx.fomc and p.fomc_mode == "half":
            self.size_mult = 0.5
        for b in warmup or []:
            self._agg5(b, warm=True)
        ev = {"type": "brief", "ts": None, "ctx": ctx.to_dict(), "params": p.to_dict(),
              "size_mult": self.size_mult, "atr5": self.atr5,
              "learner": self.learner.summary() if self.learner else None}
        if ctx.fomc and p.fomc_mode == "skip":
            self.done_reason = "FOMC day - the bot sits this session out"
        return [ev]

    def end_day(self) -> list[dict]:
        out: list[dict] = []
        last = self.prev_bar
        if self.trade is not None and last is not None:
            out += self._exit(self.trade, last.close, last.end, "closed", "session end")
        for i in self.ideas:
            if i.state in ("watch", "armed"):
                i.state = "expired"
                if i.posted:
                    out.append(self._idea_ev("idea_cancel", i, last.end if last else None, "session ended without a trigger"))
        out.append({"type": "session_closed", "ts": last.end if last else None})
        out.append(self._summary(last.end if last else None))
        return out

    def _summary(self, ts) -> dict:
        tr = self.trades
        wins = [t for t in tr if (t.pts or 0) > 0.5]
        losses = [t for t in tr if (t.pts or 0) < -0.5]
        return {"type": "day_summary", "ts": ts, "trades": [t.to_dict() for t in tr], "n": len(tr),
                "wins": len(wins), "losses": len(losses), "be": len(tr) - len(wins) - len(losses),
                "net_pts": round(sum(t.pts or 0 for t in tr), 2), "net_r": round(sum(t.r or 0 for t in tr), 2),
                "ideas": len(self.ideas), "done_reason": self.done_reason}

    # ================================================================== bars
    def on_bar(self, bar: Bar) -> list[dict]:
        p = self.p
        out: list[dict] = []
        t = bar.ts.time()
        if t < time(9, 30) or t >= time(16, 0):
            return out
        if self.open_px is None:
            self.open_px = bar.open
        self._update_vwap(bar)
        closed5 = self._agg5(bar)
        # opening range
        or_end = (datetime.combine(bar.ts.date(), time(9, 30), bar.ts.tzinfo) + timedelta(minutes=p.or_minutes)).time()
        if t < or_end:
            self.or_hi = bar.high if self.or_hi is None else max(self.or_hi, bar.high)
            self.or_lo = bar.low if self.or_lo is None else min(self.or_lo, bar.low)
        elif not self.or_done and self.or_hi is not None:
            self.or_done = True

        # manage the open trade first (entry bar itself is not re-tested)
        if self.trade is not None:           # entries happen after this point, so the entry bar is never re-tested
            out += self._manage(self.trade, bar)
        # flat time
        if not self.flat_done and _mins(t) + bar.minutes > _mins(_t(p.flat_time)):
            self.flat_done = True
            if self.trade is not None:
                out += self._exit(self.trade, bar.close, bar.end, "closed", "flat time")
        # window close
        in_window = _mins(_t(p.entry_start)) <= _mins(t) < _mins(_t(p.entry_end))
        if not self.window_closed and _mins(t) >= _mins(_t(p.entry_end)):
            self.window_closed = True
            out += self._cancel_all(bar.end, "entry window closed")
            if not self.done_reason:
                self.done_reason = f"entry window closed ({p.entry_end} ET)"
                out.append(self._done_ev(bar.end))

        if self.atr5 and not self.done_reason and in_window:
            out += self._update_ideas(bar, closed5)
            if closed5 is not None:
                out += self._detect5(closed5, bar)
            out += self._detect_vwap(bar)
        self.prev_bar = bar
        self.bars.append(bar)
        return out

    # ------------------------------------------------------------------ indicators
    def _agg5(self, bar: Bar, warm: bool = False) -> Bar | None:
        """Aggregate base bars into 5-minute bars; returns the 5m bar that just closed (if any)."""
        bucket = bar.ts.replace(minute=bar.ts.minute - bar.ts.minute % 5, second=0, microsecond=0)
        if bar.minutes >= 5:
            done = Bar(bucket, bar.open, bar.high, bar.low, bar.close, bar.volume, 5)
            self._push5(done)
            return None if warm else done
        f = self.five
        if f is None or f.ts != bucket:
            f = self.five = Bar(bucket, bar.open, bar.high, bar.low, bar.close, bar.volume, 5)
        else:
            f.high, f.low, f.close = max(f.high, bar.high), min(f.low, bar.low), bar.close
            f.volume += bar.volume
        if bar.end >= bucket + timedelta(minutes=5):
            self.five = None
            self._push5(f)
            return None if warm else f
        return None

    def _push5(self, b: Bar) -> None:
        prev = self.fives[-1].close if self.fives else None
        tr = b.high - b.low if prev is None else max(b.high - b.low, abs(b.high - prev), abs(b.low - prev))
        n = self.p.atr_len
        if self.atr5 is None:
            self._tr_seed.append(tr)
            if len(self._tr_seed) >= min(n, 6):
                self.atr5 = sum(self._tr_seed) / len(self._tr_seed)
        else:
            self.atr5 = (self.atr5 * (n - 1) + tr) / n
        self.fives.append(b)

    def _update_vwap(self, bar: Bar) -> None:
        tp = (bar.high + bar.low + bar.close) / 3
        if bar.volume and bar.volume > 0:
            self.cum_pv += tp * bar.volume
            self.cum_v += bar.volume
            self.vwap = self.cum_pv / self.cum_v
        else:                                   # proxy feeds without volume: time-weighted average price
            self.cum_tp += tp
            self.cum_v += 1
            self.vwap = self.cum_tp / self.cum_v
        side = 1 if bar.close > self.vwap else -1 if bar.close < self.vwap else 0
        if side:
            if self.vwap_side and side != self.vwap_side:
                self.vwap_crosses += 1
            self.vwap_side = side
            self.side_counts[side] += 1
        if self.prev_bar is not None:
            self.path_len += abs(bar.close - self.prev_bar.close)
        self.vwap_hist.append((bar.ts, self.vwap))

    def session_er(self, px: float) -> float:
        if not self.path_len or self.open_px is None:
            return 0.0
        return abs(px - self.open_px) / self.path_len

    def regime(self, now: datetime, px: float) -> str:
        if self._news_window(now, after=60) or (self.ctx and self.ctx.fomc):
            return "news"
        return "trend" if self.session_er(px) >= 0.3 and self.vwap_crosses <= 2 else "range"

    def _news_window(self, now: datetime, before: int | None = None, after: int | None = None):
        if not self.ctx:
            return None
        before = self.p.news_before_min if before is None else before
        after = self.p.news_after_min if after is None else after
        m = _mins(now.time())
        for e in self.ctx.events:
            if (e.get("impact") or "high") != "high" or not e.get("time"):
                continue
            em = _mins(_t(e["time"]))
            if em - before <= m < em + after:
                return e
        return None

    # ------------------------------------------------------------------ idea detection
    def _levels(self, include_or: bool = True) -> dict[str, float]:
        lv = dict(self.ctx.levels()) if self.ctx else {}
        if include_or and self.or_done:
            lv["ORH"], lv["ORL"] = self.or_hi, self.or_lo
        return lv

    def _new_idea(self, setup, side, level_name, level, zone, optimal, stop, now, reasons, extreme=None, state="armed"):
        key = f"{setup}:{level_name}:{side}"
        if key in self.seen_keys:
            return None
        self.seen_keys.add(key)
        r = self._round
        i = Idea(0, setup, side, level_name, level, r(min(zone)), r(max(zone)), r(optimal), r(stop), now,
                 now + timedelta(minutes=self.p.idea_expiry_min), key, reasons, extreme, state)
        self.ideas.append(i)
        return i

    def _number(self, i: Idea) -> int:
        """Members see #1, #2 ... for ideas they were told about and trades - silent watches don't use numbers."""
        if not i.id:
            i.id = self.next_id
            self.next_id += 1
        return i.id

    def _detect5(self, f: Bar, bar: Bar) -> list[dict]:
        p, a = self.p, self.atr5
        out = []
        body = abs(f.close - f.open)
        # ---- opening-range breakout -> retest idea
        if "orb" in p.setups and self.or_done and self.or_hi is not None:
            rng = f.high - f.low or 1e-9
            for side in (1, -1):
                edge = self.or_hi if side > 0 else self.or_lo
                broke = (f.close - edge) * side > 0.05 * a
                strong = body >= p.disp_body_atr * a and ((f.close - f.low) / rng if side > 0 else (f.high - f.close) / rng) >= 0.6
                if broke and strong and (f.close - f.open) * side > 0:
                    zone = (edge - side * 0.10 * a, edge + side * p.zone_atr * a)
                    opt = edge + side * 0.05 * a
                    stop = min(zone) - p.stop_atr * a if side > 0 else max(zone) + p.stop_atr * a
                    why = [f"5-min candle closed {'above' if side > 0 else 'below'} the opening range "
                           f"{'high' if side > 0 else 'low'} {edge:,.2f} with a {body:.0f}-pt body ({body / a:.1f}x ATR)",
                           "waiting for the retest of the breakout level - no chasing"]
                    i = self._new_idea("orb", side, "ORH" if side > 0 else "ORL", edge, zone, opt, stop, f.end, why)
                    if i:
                        i.posted = True
                        self._number(i)
                        out.append(self._idea_ev("idea", i, f.end))
        # ---- sweep confirmation on the 5m close
        for i in self.ideas:
            if i.setup != "sweep" or i.state != "watch":
                continue
            back = (f.close - i.level) * i.side > 0          # closed back on our side of the level
            disp = (f.close - f.open) * i.side >= 0.7 * p.disp_body_atr * a
            if back and disp:
                stop = i.extreme - i.side * p.stop_buffer_atr * a - i.side * p.tick
                out += self._try_enter(i, bar, f.close, stop, f.end,
                                       extra=[f"5-min candle closed back {'above' if i.side > 0 else 'below'} "
                                              f"{i.level_name} {i.level:,.2f} with a {abs(f.close - f.open):.0f}-pt body"])
        return out

    def _sweep_watch(self, bar: Bar) -> list[dict]:
        p, a = self.p, self.atr5
        out = []
        if "sweep" not in p.setups:
            return out
        prev_close = self.prev_bar.close if self.prev_bar else self.open_px
        for name, lv in self._levels().items():
            if name == "PDC":
                continue
            for side in (1, -1):        # side = trade direction; a long comes from a sweep BELOW a level
                beyond = (lv - bar.low) if side > 0 else (bar.high - lv)
                came_from = (prev_close - lv) * side > 0     # previous close was on the reclaim side
                if beyond >= max(p.sweep_min_atr * a, 3 * p.tick) and came_from:
                    ext = bar.low if side > 0 else bar.high
                    why = [f"price ran {'below' if side > 0 else 'above'} {name} {lv:,.2f} by {beyond:.0f} pts - "
                           f"stops there just got taken", "watching for a 5-min close back "
                           f"{'above' if side > 0 else 'below'} the level to confirm the reversal"]
                    zone = (lv, lv - side * 0.3 * a)
                    i = self._new_idea("sweep", side, name, lv, zone, lv, ext, bar.end, why, extreme=ext, state="watch")
                    if i:          # tracked silently: most level pokes are breakouts, not sweeps - no heads-up spam
                        i.expires = bar.end + timedelta(minutes=p.sweep_window_min)
        return out

    def _update_ideas(self, bar: Bar, closed5: Bar | None) -> list[dict]:
        p, a = self.p, self.atr5
        out = self._sweep_watch(bar)
        for i in self.ideas:
            if i.state not in ("watch", "armed"):
                continue
            if bar.end >= i.expires:
                i.state = "expired"
                if i.posted:
                    out.append(self._idea_ev("idea_cancel", i, bar.end, "expired without a trigger"))
                continue
            if i.setup == "sweep":
                ext = bar.low if i.side > 0 else bar.high
                if (i.extreme - ext) * i.side > 0:
                    i.extreme = ext
                if abs(i.extreme - i.level) > p.sweep_max_atr * a:
                    i.state = "cancelled"
                    if i.posted:
                        out.append(self._idea_ev("idea_cancel", i, bar.end, f"price kept going {abs(i.extreme - i.level):.0f} pts "
                                                 f"past {i.level_name} - that's a breakout, not a sweep"))
                continue
            # orb retest
            if closed5 is not None and self.or_hi is not None:
                mid = (self.or_hi + self.or_lo) / 2
                if (closed5.close - mid) * i.side < 0:
                    i.state = "cancelled"
                    out.append(self._idea_ev("idea_cancel", i, bar.end, "breakout failed - price closed back inside the range"))
                    continue
            touched = (bar.low <= i.zone_hi) if i.side > 0 else (bar.high >= i.zone_lo)
            held = (bar.close >= i.zone_lo) if i.side > 0 else (bar.close <= i.zone_hi)
            turned = (bar.close - bar.open) * i.side > 0
            if touched and held and turned:
                out += self._try_enter(i, bar, bar.close, i.stop, bar.end,
                                       extra=[f"retest held: price dipped into {i.zone_lo:,.2f}-{i.zone_hi:,.2f} and closed back "
                                              f"{'up' if i.side > 0 else 'down'}"])
        return out

    def _detect_vwap(self, bar: Bar) -> list[dict]:
        p, a = self.p, self.atr5
        if "vwap" not in p.setups or self.vwap is None or bar.ts.time() < _t(p.vwap_start) or self.trade is not None:
            return []
        n = sum(self.side_counts.values())
        if n < 15:
            return []
        side = 1 if self.side_counts[1] >= 0.8 * n else -1 if self.side_counts[-1] >= 0.8 * n else 0
        if not side or self.session_er(bar.close) < 0.3:
            return []
        past = [v for ts, v in self.vwap_hist if ts <= bar.ts - timedelta(minutes=20)]
        if not past or (self.vwap - past[-1]) * side <= 0:
            return []
        prev = self.prev_bar
        touched = (bar.low <= self.vwap + 0.15 * a) if side > 0 else (bar.high >= self.vwap - 0.15 * a)
        held = (bar.close - self.vwap) * side > 0
        turned = prev is not None and (bar.close - (prev.high if side > 0 else prev.low)) * side > 0
        if not (touched and held and turned):
            return []
        recent = self.bars[-3:] + [bar]
        swing = min(b.low for b in recent) if side > 0 else max(b.high for b in recent)
        stop = swing - side * p.stop_buffer_atr * a
        why = [f"one-sided session: {max(self.side_counts.values())}/{n} bars on the "
               f"{'buy' if side > 0 else 'sell'} side of VWAP, efficiency {self.session_er(bar.close):.2f}",
               f"first pullback to VWAP {self.vwap:,.2f} closed back {'up' if side > 0 else 'down'}"]
        i = self._new_idea("vwap", side, "VWAP", self.vwap, (self.vwap, bar.close), bar.close, stop, bar.end, why)
        if not i:
            return []
        return self._try_enter(i, bar, bar.close, stop, bar.end, direct=True)

    # ------------------------------------------------------------------ scoring and entry
    def _score(self, i: Idea, risk: float, final_r: float, now: datetime, regime: str) -> tuple[float, list[str]]:
        s, why = 50.0, []
        if i.setup in ("sweep", "orb") and i.level_name in ("PDH", "PDL", "ONH", "ONL"):
            s += 10
            why.append(f"key level ({i.level_name})")
        if _mins(now.time()) < 600:
            s += 10
            why.append("first 30 minutes - historically the cleanest window")
        b = self.ctx.bias if self.ctx else 0
        if b and b == i.side:
            s += 5
            why.append(f"with the day's {self.ctx.bias_label.lower()} lean")
        elif b and b != i.side:
            s -= 8
            why.append(f"against the day's {self.ctx.bias_label.lower()} lean")
        if final_r >= 2.5:
            s += 5
        if i.setup != "sweep" and self.vwap_crosses >= 4:
            s -= 15
            why.append(f"choppy open ({self.vwap_crosses} VWAP crosses)")
        if self.learner:
            adj, note = self.learner.adjust(i.setup, regime)
            s += adj
            if note:
                why.append(note)
        return s, why

    def _final_target(self, side: int, entry: float, risk: float) -> tuple[float, str]:
        p = self.p
        cands = []
        for name, lv in self._levels().items():
            d = (lv - entry) * side
            if d >= p.min_final_r * risk:
                cands.append((d, name, lv))
        if self.vwap is not None:
            d = (self.vwap - entry) * side
            if d >= p.min_final_r * risk:
                cands.append((d, "VWAP", self.vwap))
        if cands:
            d, name, lv = min(cands)
            if d <= p.max_final_r * risk:
                return self._round(lv - side * p.tick), name
        return self._round(entry + side * p.final_r * risk), f"{p.final_r:g}R"

    def _round(self, x: float) -> float:
        return round(x / self.p.tick) * self.p.tick

    def _gate(self, now: datetime) -> str | None:
        p = self.p
        if self.trade is not None:
            return "a trade is already open"
        if self.done_reason:
            return self.done_reason
        if len(self.trades) >= p.max_trades:
            return f"max {p.max_trades} trades reached"
        e = self._news_window(now)
        if e:
            return f"news blackout ({e['name']} at {e['time']} ET)"
        return None

    def _try_enter(self, i: Idea, bar: Bar, px: float, stop: float, now: datetime, extra=None, direct=False) -> list[dict]:
        p, out = self.p, []
        why_not = self._gate(now)
        side = i.side
        slip = p.slippage_ticks * p.tick
        entry = self._round(px + side * slip)
        stop = self._round(stop)
        risk = (entry - stop) * side
        if why_not is None and risk < p.min_risk_pts:
            stop = self._round(entry - side * p.min_risk_pts)
            risk = p.min_risk_pts
        if why_not is None and risk > p.max_risk_pts:
            why_not = f"stop would be {risk:.0f} pts away (max {p.max_risk_pts:g})"
        edge = i.zone_hi if side > 0 else i.zone_lo
        if why_not is None and i.setup == "orb" and (px - edge) * side > p.chase_r * max(risk, 1e-9):
            why_not = f"price is already {(px - edge) * side:.0f} pts past the entry zone - not chasing"
        final, final_name = self._final_target(side, entry, risk) if why_not is None else (None, "")
        regime = self.regime(now, px)
        score, swhy = self._score(i, risk, (final - entry) * side / risk if final else 0, now, regime) if why_not is None else (0, [])
        if why_not is None and self.learner and not self.learner.allowed(i.setup, regime):
            why_not = f"{i.setup} setups are underperforming in {regime} conditions lately (adaptive filter)"
        if why_not is None and score < p.min_score:
            why_not = f"score {score:.0f} below {p.min_score:.0f}"
        if why_not is not None:
            i.state = "cancelled"
            if i.posted:
                out.append(self._idea_ev("idea_cancel", i, now, why_not))
            return out
        i.state = "validated"
        self._number(i)
        self.used_keys.add(i.key)
        t = Trade(i.id, i.setup, side, entry, stop, stop, risk, self._round(entry + side * p.t1_r * risk), final, final_name,
                  now, (round(i.zone_lo, 2), round(i.zone_hi, 2)), round(i.optimal, 2), round(score, 1), regime,
                  i.reasons + (extra or []) + swhy, i.key, size=self.size_mult, level_name=i.level_name, from_idea=i.posted)
        self.trade = t
        out.append({"type": "signal", "ts": now, "trade": t.to_dict(), "direct": not i.posted, "price": px,
                    "past_optimal": round((px - i.optimal) * side, 2), "atr5": round(self.atr5, 2),
                    "vwap": round(self.vwap, 2) if self.vwap else None, "or": [self.or_lo, self.or_hi]})
        return out

    # ------------------------------------------------------------------ management
    def _manage(self, t: Trade, bar: Bar) -> list[dict]:
        p, out = self.p, []
        s = t.side
        fav = (bar.high if s > 0 else bar.low)
        adv = (bar.low if s > 0 else bar.high)
        t.mfe = max(t.mfe, (fav - t.entry) * s)
        t.mae = max(t.mae, (t.entry - adv) * s)
        hit_stop = (adv - t.stop) * s <= 0
        if not t.t1_hit:
            if hit_stop:
                return self._exit(t, t.stop - s * p.slippage_ticks * p.tick, bar.end, "stopped", "stop hit")
            if not t.heads_up and t.mfe >= 0.7 * t.risk and (fav - t.t1) * s < 0:
                t.heads_up = True
                rng = sum(f.high - f.low for f in self.fives[-6:]) / max(1, len(self.fives[-6:]))
                out.append({"type": "heads_up", "ts": bar.end, "id": t.id, "trade": t.to_dict(),
                            "mfe": round(t.mfe, 2), "candle": round(rng, 1), "regime": self.regime(bar.end, bar.close)})
            if (fav - t.t1) * s >= 0:
                t.t1_hit = True
                frac = p.scale_frac
                t.banked = (t.t1 - t.entry) * s * frac
                old = t.stop
                if p.be_mode == "t1":
                    t.stop, t.be_active = t.entry, True
                elif p.be_mode == "lock":
                    t.stop = self._round(t.entry - s * 0.25 * t.risk)
                out.append({"type": "t1", "ts": bar.end, "id": t.id, "trade": t.to_dict(), "pts": round((t.t1 - t.entry) * s, 2),
                            "old_stop": old, "new_stop": t.stop, "be_mode": p.be_mode})
                out.append({"type": "scaleout", "ts": bar.end, "id": t.id, "trade": t.to_dict(), "frac": frac,
                            "pts": round((t.t1 - t.entry) * s, 2)})
                if (fav - t.final) * s >= 0:
                    return out + self._exit(t, t.final, bar.end, "final", "final target")
            return out
        # runner
        if hit_stop:
            reason = "breakeven" if abs(t.stop - t.entry) < 1e-9 else ("closed" if (t.stop - t.entry) * s > 0 else "stopped")
            return out + self._exit(t, t.stop - s * p.slippage_ticks * p.tick, bar.end, reason, "runner stop")
        if (fav - t.final) * s >= 0:
            return out + self._exit(t, t.final, bar.end, "final", "final target")
        if p.be_mode == "t1_close" and not t.be_active and (bar.close - t.t1) * s > 0:
            t.stop, t.be_active = t.entry, True
            out.append({"type": "stop_moved", "ts": bar.end, "id": t.id, "trade": t.to_dict(), "new_stop": t.stop,
                        "why": "a candle closed beyond Target 1 - stop to break-even now"})
        if p.be_mode == "lock" and t.mfe >= 1.5 * t.risk and (t.stop - t.entry) * s < 0.5 * t.risk:
            t.stop = self._round(t.entry + s * 0.5 * t.risk)
            out.append({"type": "stop_moved", "ts": bar.end, "id": t.id, "trade": t.to_dict(), "new_stop": t.stop,
                        "why": "+1.5R reached - locking half the risk in profit"})
        if t.mfe >= 1.5 * t.risk and self.atr5:
            trail = self._round(t.entry + s * (t.mfe - p.trail_atr * self.atr5))
            if (trail - t.stop) * s > 0 and (trail - t.entry) * s >= 0:
                t.stop = trail
        step = int((t.mfe - t.risk) // (0.5 * t.risk)) if t.mfe > t.risk else 0
        if step > t.runner_step and (t.last_runner is None or bar.end - t.last_runner >= timedelta(minutes=5)):
            t.runner_step, t.last_runner = step, bar.end
            out.append({"type": "runner", "ts": bar.end, "id": t.id, "trade": t.to_dict(), "open_pts": round(t.open_pts(bar.close), 2),
                        "mfe": round(t.mfe, 2), "to_final": round((t.final - bar.close) * s, 2), "stop": t.stop,
                        "lock": round(max(0.0, (t.stop - t.entry) * s), 2)})
        return out

    def _exit(self, t: Trade, px: float, ts, reason: str, why: str) -> list[dict]:
        s = t.side
        px = self._round(px)
        if t.t1_hit:
            pts = t.banked + (px - t.entry) * s * (1 - self.p.scale_frac)
        else:
            pts = (px - t.entry) * s
        t.exit, t.exit_reason, t.closed = px, reason, ts
        t.pts = round(pts, 2)
        t.r = round(pts / t.risk, 3) if t.risk else 0.0
        self.trade = None
        self.trades.append(t)
        self.history.append(t)
        self.day_r += t.r * t.size
        out = [{"type": reason, "ts": ts, "id": t.id, "trade": t.to_dict(), "pts": t.pts, "r": t.r, "why": why}]
        if self.learner:
            self.learner.record(t.setup, t.regime, t.r, ts)
        if t.pts < -0.5:
            self.lost = True
        if not self.done_reason:
            if self.p.stop_after_loss and self.lost:
                self.done_reason = "first loss of the day - the rules say stop (it protects the account)"
            elif len(self.trades) >= self.p.max_trades:
                self.done_reason = f"{self.p.max_trades} trades taken - daily maximum"
            elif self.day_r <= -self.p.max_daily_loss_r:
                self.done_reason = f"daily loss limit ({self.p.max_daily_loss_r:g}R) reached"
            if self.done_reason:
                out += self._cancel_all(ts, "done for the day")
                out.append(self._done_ev(ts))
        return out

    def _cancel_all(self, ts, why) -> list[dict]:
        out = []
        for i in self.ideas:
            if i.state in ("watch", "armed"):
                i.state = "cancelled"
                if i.posted:
                    out.append(self._idea_ev("idea_cancel", i, ts, why))
        return out

    def _done_ev(self, ts) -> dict:
        return {"type": "done", "ts": ts, "why": self.done_reason,
                "open": self.trade.to_dict() if self.trade else None}

    def _idea_ev(self, kind: str, i: Idea, ts, why: str | None = None) -> dict:
        risk = abs(i.optimal - i.stop)
        return {"type": kind, "ts": ts, "id": i.id, "setup": i.setup, "side": i.side, "level_name": i.level_name,
                "level": round(i.level, 2), "zone": [round(i.zone_lo, 2), round(i.zone_hi, 2)], "optimal": round(i.optimal, 2),
                "stop": round(i.stop, 2), "risk": round(risk, 2), "reasons": list(i.reasons), "why": why,
                "price": self.prev_bar.close if self.prev_bar else None, "state": i.state,
                "counter_bias": bool(self.ctx and self.ctx.bias and self.ctx.bias != i.side)}


def sanity_bounds(x: float) -> bool:
    return x is not None and math.isfinite(x)

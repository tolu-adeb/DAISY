"""Pre-session context for a trading day, built only from data available before 9:30 ET.

Levels: prior RTH high/low/close (PDH/PDL/PDC), overnight Globex high/low (ONH/ONL, 18:00-9:30),
daily ATR.  Bias: a soft lean from the prior days' trend and where the overnight session left price
relative to yesterday's range.  On this data the daily features did not separate good from bad days
(see docs/14), so bias only nudges the score - it never blocks a setup.  Macro events come from the
terminal's calendar (CPI, jobs, FOMC ...).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from typing import Any

import numpy as np
import pandas as pd

from .data import NY, is_rth, split_sessions


@dataclass
class DayContext:
    day: date
    pdh: float | None = None
    pdl: float | None = None
    pdc: float | None = None
    onh: float | None = None
    onl: float | None = None
    open_ref: float | None = None          # last price before 9:30
    atr_d: float | None = None             # daily ATR(10), points
    trend_d: int = 0                       # +1 / -1 / 0 from prior closes vs their 10-day mean
    er5: float | None = None               # 5-day efficiency ratio of daily closes
    bias: int = 0
    bias_label: str = "Mixed"
    bias_reasons: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)      # [{"time": "10:00", "name": ..., "impact": "high"}]
    fomc: bool = False
    notes: list[str] = field(default_factory=list)

    def levels(self) -> dict[str, float]:
        out = {"PDH": self.pdh, "PDL": self.pdl, "PDC": self.pdc, "ONH": self.onh, "ONL": self.onl}
        return {k: v for k, v in out.items() if v is not None and np.isfinite(v)}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["day"] = str(self.day)
        return d


def _atr(daily: pd.DataFrame, n: int = 10) -> float | None:
    if len(daily) < 3:
        return None
    pc = daily["close"].shift()
    tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(), (daily["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


def build_context(day: date, prior_daily: pd.DataFrame, overnight: pd.DataFrame | None = None,
                  events: list[dict] | None = None) -> DayContext:
    """``prior_daily``: daily bars strictly before ``day``; ``overnight``: that session's bars before 9:30."""
    ctx = DayContext(day=day, events=events or [])
    ctx.fomc = any("FOMC" in (e.get("name") or "") for e in ctx.events)
    if len(prior_daily):
        last = prior_daily.iloc[-1]
        ctx.pdh, ctx.pdl, ctx.pdc = float(last["high"]), float(last["low"]), float(last["close"])
        ctx.atr_d = _atr(prior_daily)
        c = prior_daily["close"]
        if len(c) >= 5:
            ma = c.tail(10).mean()
            ctx.trend_d = int(np.sign(c.iloc[-1] - ma))
            moves = c.diff().abs().tail(5).sum()
            ctx.er5 = float(abs(c.iloc[-1] - c.iloc[-6]) / moves) if len(c) >= 6 and moves > 0 else None
    if overnight is not None and len(overnight):
        ctx.onh, ctx.onl = float(overnight["high"].max()), float(overnight["low"].min())
        ctx.open_ref = float(overnight["close"].iloc[-1])
    _bias(ctx)
    return ctx


def _bias(ctx: DayContext) -> None:
    score, why = 0, []
    if ctx.trend_d:
        score += ctx.trend_d
        why.append(f"prior closes {'above' if ctx.trend_d > 0 else 'below'} their 10-day average")
    ref = ctx.open_ref
    if ref is not None and ctx.pdh is not None and ctx.pdl is not None:
        if ref > ctx.pdh:
            score += 1
            why.append(f"overnight trading above yesterday's high {ctx.pdh:,.2f}")
        elif ref < ctx.pdl:
            score -= 1
            why.append(f"overnight trading below yesterday's low {ctx.pdl:,.2f}")
        elif ctx.pdc is not None:
            score += 1 if ref > ctx.pdc else -1
            why.append(f"overnight {'above' if ref > ctx.pdc else 'below'} yesterday's close {ctx.pdc:,.2f}")
    ctx.bias = int(np.sign(score)) if abs(score) >= 2 else 0
    ctx.bias_label = {1: "Bullish", -1: "Bearish", 0: "Mixed"}[ctx.bias]
    ctx.bias_reasons = why


def contexts_from_intraday(bars: pd.DataFrame, daily: pd.DataFrame | None = None,
                           events_for=None) -> dict[date, tuple[DayContext, pd.DataFrame]]:
    """For each session in ``bars``: (context, that session's RTH bars).  Daily levels come from
    ``daily`` when given (e.g. a long daily CSV), else from the intraday bars' own RTH sessions."""
    from .data import rth_daily
    sessions = split_sessions(bars)
    own_daily = rth_daily(bars)
    out = {}
    for d, g in sorted(sessions.items()):
        src = daily if daily is not None and len(daily) else own_daily
        prior = src[src.index < pd.Timestamp(d)]
        pre = g[[t.time() < time(9, 30) or t.time() >= time(18, 0) for t in g.index]]
        rth = g[[is_rth(t) for t in g.index]]
        if rth.empty:
            continue
        ev = events_for(d) if events_for else []
        out[d] = (build_context(d, prior, pre if len(pre) else None, ev), rth)
    return out


def calendar_events(day: date, settings=None) -> list[dict]:
    """High/medium-impact US macro events for ``day`` from the terminal's calendar (ET times)."""
    try:
        from ..markets.calendar import Calendar, builtin_events
        uf = Calendar(settings=settings).user_file if settings is not None else None
        evs = builtin_events(day, day, uf)
    except Exception:
        return []
    return [{"time": e.time, "name": e.name, "impact": e.impact} for e in evs if e.time]


def minutes_of(t: time) -> int:
    return t.hour * 60 + t.minute


def now_et() -> datetime:
    return datetime.now(NY)

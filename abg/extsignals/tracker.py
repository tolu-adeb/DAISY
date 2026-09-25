"""Orchestrates external trade ideas: ingest → validate → track → decide → explain → relay.

    message ──parse──► update? ──► apply to the idea it replies to / the latest open idea
                  └──► idea ──► validate vs live quote & analysis (default stop/targets, sizing,
                                expiry) ──► store ──► "ingested" event (+ immediate entry if the
                                price is already in the zone)
    quote sweep ──► on_quotes ──► lifecycle.step per open idea (grade gate on entries)
    analysis sweep ──► review ──► re-grade pending ideas, advisories for deteriorating trades
    start-up ──► catch_up ──► replay daily bars missed while nothing was running

Every event is explained with live context (commentary.explain), stored in the idea's timeline,
published to the notification hub (dashboard / desktop / email) and relayed back to the source
channel (discord.Relay), where a Discord relay replaces the hub's generic Discord embed.
All tracking is *paper*: nothing here places orders.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import fields
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..analysis import forecast as fcast
from ..engine import AnalysisEngine, AnalyzeOptions
from ..errors import ABGError
from ..live.market_hours import is_market_open, is_trading_day
from ..live.signals import Signal
from ..models import Quote
from ..utils import jsonable, normalize_symbol
from . import commentary as cm
from .lifecycle import OPEN_STATES, Idea, Obs, manual_exit, step, summary_stats, trigger_fill
from .parser import ParsedSignal, parse
from .store import ExtSignalStore

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
_QF = {f.name for f in fields(Quote)}

SEVERITY = {"entry": "warning", "target_hit": "warning", "stop_hit": "critical", "trailing_stop": "warning",
            "breakeven_stop": "warning", "time_exit": "warning", "exit": "warning", "trim": "warning",
            "entry_blocked": "warning", "advisory": "warning"}
BARRIER_KINDS = {"ingested", "approaching", "entry", "entry_blocked", "target_hit", "advisory"}
# (entry expiry days, max hold days, barrier horizon in trading days); swing uses the settings
TIMEFRAMES = {"scalp": (1, 2, 3), "day": (2, 3, 5), "position": (None, None, 90)}


def _quote(d: dict | None) -> Quote | None:
    try:
        return Quote(**{k: v for k, v in (d or {}).items() if k in _QF}) if d and not d.get("error") else None
    except Exception:
        return None


class ExtSignalTracker:
    def __init__(self, engine: AnalysisEngine, store: ExtSignalStore, hub=None, relay=None, settings=None):
        self.engine, self.store, self.hub, self.relay = engine, store, hub, relay
        self.s = settings or engine.settings
        self._ctx: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._relay_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        kinds = [k.strip() for k in (self.s.ext_relay_kinds or "").split(",") if k.strip()]
        self.relay_kinds = set(kinds) if kinds else None
        self.last_quotes_at: float | None = None
        self.last_review_at: float | None = None

    # ================================================================== context
    def symbols(self) -> list[str]:
        return sorted({i.symbol for i in self.store.open_ideas()})

    async def context(self, sym: str, quote: Quote | dict | None = None, max_age: float = 600) -> dict | None:
        """Cached analysis report + daily closes for a symbol (never raises)."""
        c = self._ctx.get(sym)
        if c and time.time() - c["ts"] < max_age:
            return c
        q = quote if isinstance(quote, Quote) else _quote(quote)
        try:
            r = await self.engine.analyze(sym, AnalyzeOptions(
                period="1y", ai=False, options=False, news=True, fundamentals=True, benchmark=True,
                forecast_paths=2000, live_quote=q, history_ttl=self.s.monitor_history_ttl))
            h = await self.engine.history(sym, "2y", "1d", ttl=self.s.monitor_history_ttl)
        except ABGError as e:
            log.warning("context for %s failed: %s", sym, e.message)
            return c
        except Exception:  # pragma: no cover - defensive
            log.exception("context for %s failed", sym)
            return c
        c = {"ts": time.time(), "report": r, "close": h.value.df["close"]}
        self._ctx[sym] = c
        return c

    async def barrier(self, idea: Idea, ctx: dict | None, price: float | None) -> dict | None:
        """Monte Carlo odds of the next target before the stop, from the idea's own levels."""
        if not ctx or idea.stop is None or not idea.targets:
            return None
        pending = [t for j, t in enumerate(idea.targets) if j not in idea.targets_hit]
        if not pending:
            return None
        if idea.status == "active" and price:
            start = price
        elif idea.entry_type == "zone" and idea.entry_low is not None:
            start = (idea.entry_low + idea.entry_high) / 2
        else:
            start = idea.entry_low or price
        if not start:
            return None
        lo_ok = (idea.stop < start < pending[0]) if idea.long else (pending[0] < start < idea.stop)
        if not lo_ok:
            return None
        close = ctx["close"].dropna()
        if len(close) < 150:
            return None
        r = ctx["report"] or {}
        horizon = int(idea.flags.get("horizon_td") or 30)
        cfg = fcast.ForecastConfig(paths=1500, primary_horizon=horizon, equity_premium=self.s.forecast_equity_premium,
                                   signal_tilt=self.s.forecast_signal_tilt)
        play = {"name": f"idea #{idea.id}", "direction": idea.direction,
                "levels": {"stop": idea.stop, "target_1": pending[0], "target_2": pending[1] if len(pending) > 1 else None}}
        try:
            out = await asyncio.to_thread(
                fcast.simulate, close * (start / float(close.iloc[-1])), symbol=idea.symbol, rf=self.s.risk_free_rate,
                beta=(r.get("statistics") or {}).get("beta"), signal_score=(r.get("signal") or {}).get("score"),
                sentiment=(r.get("sentiment") or {}).get("score"), plays=[play], cfg=cfg, seed=idea.id or 7)
        except Exception:  # pragma: no cover - defensive
            log.exception("barrier simulation failed for %s", idea.symbol)
            return None
        return (out.get("barriers") or [None])[0] if out.get("available") else None

    async def _regrade(self, idea: Idea, price: float | None, max_age: float = 600) -> dict:
        ctx = await self.context(idea.symbol, max_age=max_age)
        b = await self.barrier(idea, ctx, price)
        f = cm.facts(idea, (ctx or {}).get("report"), b, price)
        if ctx:
            idea.grade, idea.grade_score, idea.grade_reasons = cm.grade(idea, f)
            idea.flags["graded_at"] = time.time()
        return f

    # ================================================================== ingest
    async def ingest(self, text: str, *, source: str = "manual", channel_id: str | None = None,
                     channel_name: str | None = None, author: str | None = None, message_id: str | None = None,
                     reply_to: str | None = None, parsed: ParsedSignal | None = None) -> dict:
        """Interpret one message.  Returns {outcome, message, idea?, parsed}.

        outcomes: tracking | updated | duplicate | rejected | ignored | unmatched
        """
        p = parsed or parse(text)
        base = {"parsed": p.to_dict()}
        if message_id and channel_id and self.store.seen(channel_id, message_id):
            return {**base, "outcome": "duplicate", "message": "message already processed"}

        def record(outcome: str, idea_id: int | None = None):
            if message_id and channel_id:
                self.store.record_message(channel_id, message_id, author, text, outcome, idea_id)

        if p.kind == "update":
            res = await self.apply_update(p, text, channel_id=channel_id, author=author, reply_to=reply_to)
            record(res["outcome"], (res.get("idea") or {}).get("id"))
            return {**base, **res}
        if p.kind != "idea" or not p.trackable:
            record("ignored")
            why = "; ".join(p.warnings) or "no ticker / direction / entry found"
            return {**base, "outcome": "ignored", "message": f"not a trackable idea ({why})"}

        try:
            sym = normalize_symbol(p.symbol)
        except ABGError as e:
            record("rejected")
            return {**base, "outcome": "rejected", "message": e.message}

        quote = None
        try:
            quote = (await self.engine.quote(sym, use_cache=False)).value
        except ABGError as e:
            log.warning("no quote for %s: %s", sym, e.message)
        price = quote.price if quote else None
        ctx = await self.context(sym, quote, max_age=300)

        idea = Idea(symbol=sym, direction=p.direction, entry_type=p.entry_type, entry_low=p.entry_low,
                    entry_high=p.entry_high, stop=p.stop, targets=list(p.targets), stop_basis=p.stop_basis,
                    instrument=p.instrument, timeframe=p.timeframe, source=source, channel_id=channel_id,
                    channel_name=channel_name, author=author, message_id=message_id, raw=text[:4000],
                    parse_confidence=p.confidence, warnings=list(p.warnings))
        reject = self._prepare(idea, p, price, ctx)
        if reject is None:
            dup = self._duplicate(idea)
            if dup:
                record("duplicate", dup.id)
                return {**base, "outcome": "duplicate", "message": f"same plan already tracked as #{dup.id}",
                        "idea": self.view(dup)}

        async with self._lock:
            if reject:
                idea.status, idea.close_reason, idea.closed_at = "rejected", reject, time.time()
                self.store.save(idea)
                record("rejected", idea.id)
                await self._emit(idea, {"type": "rejected", "price": price, "reason": reject})
                return {**base, "outcome": "rejected", "message": reject, "idea": self.view(idea)}
            f = await self._regrade(idea, price)
            if price is not None:
                d = idea.distance_to_entry_pct(price)
                if d is not None and abs(d) <= self.s.ext_approach_pct:
                    idea.flags["approach_alerted"] = True       # the ingest message already says how close it is
            self.store.save(idea)
            record("tracking", idea.id)
            await self._emit(idea, {"type": "ingested", "price": price}, facts=f, extra={
                "approach_pct": self.s.ext_approach_pct,
                "expires": datetime.fromtimestamp(idea.expires_at, NY).strftime("%b %d") if idea.expires_at else "expiry"})
            if price is not None:                               # already in the zone / market entry
                await self._observe(idea, Obs(time.time(), price, price, price))
        return {**base, "outcome": "tracking", "message": f"tracking #{idea.id} {idea.symbol} {idea.direction}",
                "idea": self.view(self.store.get(idea.id) or idea)}

    def _prepare(self, idea: Idea, p: ParsedSignal, price: float | None, ctx: dict | None) -> str | None:
        """Fill defaults and validate.  Returns a rejection reason, or None."""
        s = self.s
        if idea.entry_type == "market":
            if price is None:
                return "market entry but no live quote is available"
            idea.entry_low = idea.entry_high = price
        if idea.entry_low is None:
            return "no entry level"
        if idea.entry_high is None or idea.entry_high < idea.entry_low:
            idea.entry_high = max(idea.entry_low, idea.entry_high or idea.entry_low)
        ref = idea.ref_entry
        if price is not None:
            dist = abs(ref / price - 1) * 100
            if p.instrument == "option" and dist > s.ext_max_entry_distance_pct:
                return (f"levels look like option premiums ({ref:g}) while {idea.symbol} trades at {price:,.2f}; "
                        f"only underlying price levels can be tracked")
            if dist > s.ext_max_entry_distance_pct:
                return (f"entry {ref:,.2f} is {dist:.0f}% away from the live price {price:,.2f} "
                        f"(limit {s.ext_max_entry_distance_pct:g}%); possible typo or wrong ticker")
        if p.instrument == "option":
            idea.warnings.append("option idea: tracking the underlying's price levels")
        atr = ((ctx or {}).get("report") or {}).get("indicators", {}).get("atr_14")
        if not atr and price:
            atr = price * 0.03
            idea.warnings.append("ATR unavailable: assumed 3% of price for defaults")
        if idea.stop is None:
            if not atr:
                return "no stop given and no market data to derive one"
            idea.stop = round(ref - 2 * atr if idea.long else ref + 2 * atr, 4)
            idea.flags["default_stop"] = True
        if (idea.stop >= idea.entry_low) if idea.long else (idea.stop <= idea.entry_high):
            return f"stop {idea.stop:g} is on the wrong side of the entry for a {idea.direction}"
        risk = abs(ref - idea.stop)
        if not idea.targets:
            idea.targets = [round(ref + k * risk if idea.long else ref - k * risk, 4) for k in (2, 3)]
            idea.flags["default_targets"] = True
        idea.targets = [t for t in idea.targets if (t > ref if idea.long else t < ref)]
        if not idea.targets:
            return "all targets are on the wrong side of the entry"
        idea.stop_initial = idea.stop
        # time limits
        exp_d, hold_d, horizon = TIMEFRAMES.get(idea.timeframe, (None, None, 30))
        exp_d = exp_d or s.ext_entry_expiry_days * (2 if idea.timeframe == "position" else 1)
        hold_d = hold_d or s.ext_max_hold_days * (3 if idea.timeframe == "position" else 1)
        if p.horizon_days:
            hold_d = max(hold_d if idea.timeframe != "swing" else 0, p.horizon_days * 1.5)
            horizon = max(3, int(p.horizon_days * 5 / 7))
        now = time.time()
        idea.expires_at = now + exp_d * 86400
        idea.flags["max_hold_days"] = hold_d
        idea.flags["horizon_td"] = min(252, horizon)
        self._resize(idea)
        return None

    def _resize(self, idea: Idea) -> None:
        """Paper size, fixed-fractional on the planned entry: account x risk% / risk per share."""
        risk = abs(idea.ref_entry - idea.stop) if (idea.ref_entry is not None and idea.stop is not None) else 0.0
        budget = self.s.ext_account_size * self.s.ext_risk_pct / 100
        sh = budget / risk if risk else 0.0
        idea.shares = float(math.floor(sh)) if (idea.instrument in ("stock", "etf", "option") and sh >= 1) else round(sh, 4)

    def _duplicate(self, idea: Idea) -> Idea | None:
        for o in self.store.ideas(OPEN_STATES, symbol=idea.symbol, limit=20):
            if o.direction != idea.direction or (o.author or o.channel_id) != (idea.author or idea.channel_id):
                continue
            same = lambda a, b: a is not None and b is not None and abs(a - b) <= 0.001 * max(abs(b), 1e-9)  # noqa: E731
            if same(o.entry_low, idea.entry_low) and same(o.stop_initial or o.stop, idea.stop):
                return o
        return None

    # ================================================================== live tracking
    async def on_quotes(self, quotes: dict[str, dict]) -> int:
        """Advance every open idea with the latest quotes.  Returns the number of events."""
        n = 0
        now = time.time()
        after_close = self._after_close()
        async with self._lock:
            for idea in self.store.open_ideas():
                q = quotes.get(idea.symbol) or {}
                price = q.get("price")
                if q.get("error") or not price:
                    continue
                n += await self._observe(idea, Obs(now, price, price, price))
                if (after_close and idea.status == "active" and idea.stop_basis == "close"
                        and idea.flags.get("close_checked") != after_close):
                    idea.flags["close_checked"] = after_close        # judge close-basis stops on the session close
                    n += await self._observe(idea, Obs(now, price, price, price, open=price, bar=True))
        self.last_quotes_at = now
        return n

    @staticmethod
    def _after_close() -> str | None:
        now = datetime.now(NY)
        if is_trading_day(now.date()) and now.hour >= 16 and not is_market_open():
            return now.date().isoformat()
        return None

    async def _observe(self, idea: Idea, o: Obs, allow_entry: bool | None = None, extra: dict | None = None) -> int:
        if idea.status == "active" and idea.entry_at and idea.max_hold_until is None:
            idea.max_hold_until = idea.entry_at + idea.flags.get("max_hold_days", self.s.ext_max_hold_days) * 86400
        gate = (self.s.ext_min_entry_grade or "none").strip()
        if allow_entry is None:
            allow_entry = True
            if idea.status == "pending" and gate.lower() != "none" and trigger_fill(idea, o) is not None:
                if idea.grade is None or time.time() - idea.flags.get("graded_at", 0) > self.s.ext_regrade_seconds:
                    await self._regrade(idea, o.price, max_age=min(600, self.s.ext_regrade_seconds))
                allow_entry = cm.grade_ok(idea.grade, gate)
        was_pending = idea.status == "pending"
        evs = step(idea, o, approach_pct=self.s.ext_approach_pct, move_stop_to_be=self.s.ext_move_stop_to_breakeven,
                   allow_entry=allow_entry)
        if was_pending and idea.status in ("active", "closed") and idea.entry_at:
            idea.max_hold_until = idea.entry_at + idea.flags.get("max_hold_days", self.s.ext_max_hold_days) * 86400
            idea.flags["entry_trend"] = ((self._ctx.get(idea.symbol) or {}).get("report") or {}).get("regime", {}).get("trend")
        self.store.save(idea)
        for ev in evs:
            if extra:
                ev.update(extra)
            await self._emit(idea, {**ev, "ts": o.ts if not o.bar else time.time()})
        return len(evs)

    async def catch_up(self) -> int:
        """Replay completed daily bars missed while nothing was running (e.g. PC was off for a week)."""
        today = datetime.now(NY).date()
        n = 0
        for idea in self.store.open_ideas():
            last = idea.last_checked_at or idea.created_at
            last_day = datetime.fromtimestamp(last, NY).date()
            if last_day >= today:
                continue
            try:
                ph = (await self.engine.history(idea.symbol, "6mo", "1d", ttl=3600)).value
            except ABGError as e:
                log.warning("catch-up history for %s failed: %s", idea.symbol, e.message)
                continue
            df = ph.df
            async with self._lock:
                idea = self.store.get(idea.id) or idea
                for ts, row in df.iterrows():
                    d = pd.Timestamp(ts)
                    d = (d.tz_convert(NY) if d.tzinfo else d).date()
                    if d <= last_day or d >= today or idea.status not in OPEN_STATES:
                        continue
                    bar_ts = datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY).timestamp()
                    o = Obs(bar_ts, float(row["close"]), float(row["high"]), float(row["low"]), float(row["open"]), bar=True)
                    n += await self._observe(idea, o, allow_entry=cm.grade_ok(idea.grade, self.s.ext_min_entry_grade or "none"),
                                             extra={"catch_up": d.isoformat()})
        return n

    async def review(self) -> int:
        """Analysis-sweep hook: re-grade pending ideas; advise on deteriorating active trades."""
        n = 0
        self.last_review_at = time.time()
        for idea in self.store.open_ideas():
            ctx = await self.context(idea.symbol, max_age=self.s.monitor_analysis_interval * 0.8)
            if not ctx:
                continue
            async with self._lock:
                idea = self.store.get(idea.id) or idea
                if idea.status not in OPEN_STATES:
                    continue
                price = idea.last_price or ((ctx["report"] or {}).get("quote") or {}).get("price")
                f = await self._regrade(idea, price, max_age=self.s.monitor_analysis_interval * 0.8)
                if idea.status == "active" and price:
                    n += await self._advise(idea, f, price)
                self.store.save(idea)
        return n

    async def _advise(self, idea: Idea, f: dict, price: float) -> int:
        sgn = 1 if idea.long else -1
        reasons = []
        if f["counter_trend"] and idea.flags.get("entry_trend") != f["trend"]:
            reasons.append(f"trend has turned against the trade ({f['trend']})")
        if f.get("signal_score") is not None and f["signal_score"] * sgn <= -30:
            reasons.append(f"composite signal now opposes the trade ({f['signal_score']:+.0f})")
        bad = {"Sell", "Reduce"} if idea.long else {"Buy", "Strong Buy"}
        if f.get("model_view") in bad:
            reasons.append(f"prediction model flipped to {f['model_view']}")
        if f.get("risk_level") in ("High", "Extreme"):
            reasons.append(f"baseline risk is {f['risk_level']}")
        reasons += f["opposed"][:2] if len(reasons) >= 2 else []
        key = "|".join(sorted(r.split(" (")[0] for r in reasons[:4]))
        if len([r for r in reasons if ":" not in r]) < 2:
            idea.flags.pop("advisory_key", None)
            return 0
        if idea.flags.get("advisory_key") == key and time.time() - idea.flags.get("advisory_at", 0) < 86400:
            return 0
        idea.flags["advisory_key"], idea.flags["advisory_at"] = key, time.time()
        ev = {"type": "advisory", "price": price, "reasons": reasons}
        if f.get("atr"):
            sug = price - 1.5 * f["atr"] if idea.long else price + 1.5 * f["atr"]
            if idea.stop is None or (sug > idea.stop if idea.long else sug < idea.stop):
                ev["suggested_stop"] = round(sug, 4)
        await self._emit(idea, ev, facts=f)
        return 1

    # ================================================================== updates from the source / the user
    async def apply_update(self, p: ParsedSignal, text: str, *, channel_id: str | None = None,
                           author: str | None = None, reply_to: str | None = None) -> dict:
        idea = self.store.by_message(channel_id, reply_to) if (reply_to and channel_id) else None
        if idea is None and p.symbol:
            try:
                sym = normalize_symbol(p.symbol)
            except ABGError:
                sym = None
            if sym:
                idea = self.store.latest_open_for(sym, author or channel_id) or \
                    (self.store.latest_open_for(sym) if not (author or channel_id) else None)
        if idea is None and not p.symbol and (author or channel_id):
            # no ticker and not a reply (e.g. copies from a followed channel): if this source has exactly one
            # open idea, the update can only mean that one
            mine = [i for i in self.store.open_ideas() if i.author == author and (channel_id is None or i.channel_id == channel_id)]
            if len(mine) == 1:
                idea = mine[0]
        if idea is None or idea.status not in OPEN_STATES:
            return {"outcome": "unmatched", "message": f"update '{p.action}' did not match an open tracked idea"
                    + ("" if p.symbol else " (no ticker in the message and not a reply to a tracked call)")}
        price = await self._price(idea.symbol) or idea.last_price
        note = f"source: \"{text.strip()[:120]}\""
        async with self._lock:
            idea = self.store.get(idea.id) or idea
            applied = await self._apply(idea, p.action, price, note, new_stop=p.new_stop, fraction=p.fraction,
                                        target_index=p.target_index)
        return {"outcome": "updated", "message": applied, "idea": self.view(idea)}

    async def _apply(self, idea: Idea, action: str, price: float | None, note: str, *, new_stop: float | None = None,
                     fraction: float | None = None, target_index: int | None = None) -> str:
        now = time.time()
        evs: list[dict] = []
        applied = "noted"
        if action in ("cancel", "stop_hit", "close") and idea.status == "pending":
            idea.status, idea.closed_at, idea.close_reason = "cancelled", now, f"{action.replace('_', ' ')} before entry ({note})"
            evs.append({"type": "cancelled", "price": price, "reason": idea.close_reason})
            applied = "cancelled the pending idea"
        elif action in ("cancel", "close", "stop_hit") and idea.status == "active" and price:
            reason = {"stop_hit": "source reported the stop hit", "close": "closed by the source",
                      "cancel": "cancelled by the source"}[action]
            evs.append({**manual_exit(idea, price, now, reason=reason), "note": note})
            applied = f"closed the paper position at {price:,.2f}"
        elif action == "trim" and idea.status == "active" and price:
            frac = (fraction if fraction and fraction > 0 else 0.5) * idea.remaining
            evs.append({**manual_exit(idea, price, now, fraction=frac, reason="trimmed by the source"), "note": note})
            applied = f"trimmed {frac:.0%} at {price:,.2f}"
        elif action == "target_hit" and idea.status == "active" and price:
            i = target_index if target_index is not None else (max(idea.targets_hit) + 1 if idea.targets_hit else 0)
            if i not in idea.targets_hit and i < len(idea.targets):
                n = len(idea.targets)
                frac = idea.remaining if i == n - 1 else min(idea.remaining, 1.0 / n)
                idea.targets_hit.append(i)
                ev = manual_exit(idea, price, now, fraction=frac, reason=f"source reported TP{i + 1}")
                evs.append({**ev, "type": "target_hit", "target_index": i, "target": idea.targets[i], "note": note})
                applied = f"took the TP{i + 1} slice at {price:,.2f}"
            if idea.status == "active" and (fraction == -1.0 or (i == 0 and self.s.ext_move_stop_to_breakeven)):
                evs += self._move_stop(idea, idea.entry_price, price, "breakeven (per source)")
        elif action == "breakeven" and idea.status == "active":
            evs += self._move_stop(idea, idea.entry_price, price, "breakeven (per source)")
            applied = "stop moved to breakeven"
        elif action == "move_stop" and new_stop:
            evs += self._move_stop(idea, new_stop, price, "moved by the source", allow_loosen=True)
            applied = f"stop moved to {new_stop:g}" if evs else "stop unchanged (invalid level for the current price)"
        if not evs:
            evs.append({"type": "source_update", "price": price, "action": action, "text": note, "applied": applied})
        self.store.save(idea)
        for ev in evs:
            ev.setdefault("reason", note)
            ev.setdefault("note", note)
            await self._emit(idea, {**ev, "ts": now})
        return applied

    def _move_stop(self, idea: Idea, new: float | None, price: float | None, reason: str,
                   allow_loosen: bool = False) -> list[dict]:
        if new is None or new == idea.stop:
            return []
        if price is not None and ((new >= price) if idea.long else (new <= price)) and idea.status == "active":
            return []                                  # would be an instant stop-out: ignore
        tighter = idea.stop is None or (new > idea.stop if idea.long else new < idea.stop)
        if not tighter and not allow_loosen:
            return []
        old, idea.stop = idea.stop, new
        if idea.status == "pending":               # not entered yet: the plan's 1R and size change with the stop
            idea.stop_initial = new
            self._resize(idea)
        return [{"type": "stop_moved", "price": price, "old": old, "new": new, "reason": reason}]

    # ---- user actions (CLI / API)
    async def cancel(self, idea_id: int, reason: str = "cancelled by user") -> Idea | None:
        idea = self.store.get(idea_id)
        if not idea or idea.status not in OPEN_STATES:
            return idea
        price = await self._price(idea.symbol) or idea.last_price
        async with self._lock:
            idea = self.store.get(idea_id)
            await self._apply(idea, "cancel", price, reason)
        return self.store.get(idea_id)

    async def close(self, idea_id: int, price: float | None = None, fraction: float | None = None) -> Idea | None:
        idea = self.store.get(idea_id)
        if not idea or idea.status != "active":
            return idea
        price = price or await self._price(idea.symbol) or idea.last_price
        async with self._lock:
            idea = self.store.get(idea_id)
            if fraction and fraction < 1:
                await self._apply(idea, "trim", price, "trimmed by user", fraction=fraction)
            else:
                await self._apply(idea, "close", price, "closed by user")
        return self.store.get(idea_id)

    async def edit(self, idea_id: int, *, stop: float | None = None, targets: list[float] | None = None,
                   entry_low: float | None = None, entry_high: float | None = None, stop_basis: str | None = None) -> Idea | None:
        async with self._lock:
            idea = self.store.get(idea_id)
            if not idea or idea.status not in OPEN_STATES:
                return idea
            evs, changes = [], []
            if stop is not None:
                evs += self._move_stop(idea, stop, idea.last_price, "edited by user", allow_loosen=True)
            if targets:
                ref = idea.entry_price or idea.ref_entry
                keep = [t for t in targets if (t > ref if idea.long else t < ref)]
                if keep:
                    idea.targets = sorted(keep, reverse=not idea.long)
                    changes.append("targets " + " / ".join(f"{t:g}" for t in idea.targets))
            if idea.status == "pending" and (entry_low is not None or entry_high is not None):
                lo = entry_low if entry_low is not None else idea.entry_low
                hi = entry_high if entry_high is not None else max(idea.entry_high or lo, lo)
                idea.entry_low, idea.entry_high = min(lo, hi), max(lo, hi)
                self._resize(idea)
                changes.append(f"entry {idea.entry_low:g}-{idea.entry_high:g}")
            if stop_basis in ("touch", "close"):
                idea.stop_basis = stop_basis
                changes.append(f"stop basis {stop_basis}")
            if changes:
                evs.append({"type": "source_update", "price": idea.last_price, "action": "edit",
                            "text": "edited by user", "applied": ", ".join(changes)})
            self.store.save(idea)
            for ev in evs:
                await self._emit(idea, {**ev, "ts": time.time()})
        return self.store.get(idea_id)

    async def _price(self, sym: str) -> float | None:
        try:
            return (await self.engine.quote(sym, use_cache=False)).value.price
        except ABGError:
            return None

    # ================================================================== events → timeline, hub, relay
    async def _emit(self, idea: Idea, ev: dict, facts: dict | None = None, extra: dict | None = None) -> int:
        kind = ev["type"]
        price = ev.get("price") if ev.get("price") is not None else idea.last_price
        if facts is None:
            ctx = self._ctx.get(idea.symbol)
            if kind in ("entry", "entry_blocked", "stop_hit") and (not ctx or time.time() - ctx["ts"] > 1800):
                ctx = await self.context(idea.symbol, max_age=1800)
            b = await self.barrier(idea, ctx, price) if kind in BARRIER_KINDS else None
            facts = cm.facts(idea, (ctx or {}).get("report"), b, price)
        x = cm.explain(kind, idea, {**ev, "price": price}, facts,
                       {"risk_pct": self.s.ext_risk_pct, "regrade_min": self.s.ext_regrade_seconds / 60, **(extra or {})})
        if ev.get("catch_up"):
            x["summary"] = f"(from the {ev['catch_up']} daily bar, while the monitor was offline) " + x["summary"]
        if ev.get("note") and ev["note"] not in x["summary"]:
            x["why"].insert(0, ev["note"])
        text = cm.to_text(x)
        eid = self.store.add_event(idea.id, {**ev, "price": price}, x["title"], text,
                                   jsonable({"explain": x, "event": ev, "idea": self.view(idea)}))
        relay_on = self._relay_wanted(kind)
        if self.hub is not None:
            sig = Signal(idea.symbol, f"ext_{kind}", f"{idea.id}:{kind}:{ev.get('target_index', '')}:{eid}",
                         SEVERITY.get(kind, "info"), "bullish" if idea.long else "bearish", x["title"],
                         text[:1800], data=jsonable({"price": price, "stop": idea.stop, "idea_id": idea.id,
                                                     "grade": idea.grade, "status": idea.status}),
                         portfolio=self.s.default_portfolio)
            try:
                await self.hub.publish(sig, skip={"discord"} if relay_on else None)
                self.hub.broadcast("ext", {"idea": self.view(idea), "event": {"id": eid, "type": kind, "title": x["title"]}})
            except Exception:  # pragma: no cover - never let alerts break tracking
                log.exception("hub publish failed")
        if relay_on:
            t = asyncio.ensure_future(self._relay(idea.id, eid, kind, x))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return eid

    def _relay_wanted(self, kind: str) -> bool:
        if self.relay is None or not getattr(self.relay, "enabled", False):
            return False
        if kind == "ingested" and not self.s.ext_relay_ack:
            return False
        return self.relay_kinds is None or kind in self.relay_kinds

    async def _relay(self, idea_id: int, eid: int, kind: str, x: dict) -> None:
        async with self._relay_lock:                   # keep channel order == event order
            idea = self.store.get(idea_id)
            try:
                status, ref = await asyncio.wait_for(self.relay.send(idea, kind, x), timeout=60)
            except Exception as e:
                status, ref = f"error: {getattr(e, 'message', None) or type(e).__name__}: {str(e)[:120]}", None
                log.warning("relay failed for idea %s %s: %s", idea_id, kind, status)
            self.store.mark_relayed(eid, getattr(self.relay, "name", "relay"), status)
            if ref and idea and not idea.relay_ref:
                idea = self.store.get(idea_id)
                idea.relay_ref = ref
                self.store.save(idea)

    async def drain(self, timeout: float = 30) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    # ================================================================== views
    def view(self, idea: Idea) -> dict:
        d = idea.to_dict()
        px = idea.last_price
        d["distance_pct"] = idea.distance_to_entry_pct(px) if (px and idea.status == "pending") else None
        d["open_r"] = idea.open_r(px) if (px and idea.status == "active") else 0.0
        d["total_r"] = idea.total_r(px)
        d["rr"] = [idea.rr(t) for t in idea.targets]
        d["levels"] = cm.levels_line(idea)
        return jsonable(d)

    def stats(self) -> dict:
        return jsonable(summary_stats(self.store.ideas(limit=10_000)))

    def status(self) -> dict:
        ideas = self.store.open_ideas()
        return {"open": len(ideas), "pending": sum(i.status == "pending" for i in ideas),
                "active": sum(i.status == "active" for i in ideas), "symbols": self.symbols(),
                "last_quotes_at": self.last_quotes_at, "last_review_at": self.last_review_at,
                "relay": getattr(self.relay, "describe", lambda: {"enabled": False})()}


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")

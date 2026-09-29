"""Backtesting external signals and generating training data.

``Backtester.run(messages)`` replays dated messages (a file, or a Discord channel's history) through
the same parser, validation, classification, grading and lifecycle as live tracking, day by day:

    for each trading day d:
        1. every open idea steps through its symbol's daily bar for d (stop-first on ambiguous bars,
           gap fills at the open, scale-in / scale-out, breakeven, trailing, time exits)
        2. messages dated d are applied: updates ("TP1 hit, stop to BE", "closing here") act on open
           ideas at d's close; new ideas are prepared with data known at the message time and start
           on the next bar (no look-ahead)

Grading at the signal date uses history cut at that date (indicators, composite signal, regime,
Monte Carlo odds on the idea's own levels), so "would the grade gate have helped?" is answered
honestly.  Results: every trade, stats by source / type / grade, grade calibration, an equity
curve in R, and the feature rows used to train the learned grade.

``generate_setups`` makes many more labelled trades for training: the terminal's own setup
detector run over years of history for a basket of symbols (every 5th bar), each simulated forward.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from ..analysis import indicators as ta
from ..analysis.signals import classify_plays, composite_signal, market_regime as ta_regime
from ..engine import AnalyzeOptions
from ..errors import ABGError
from ..markets.overview import regime_from_series
from ..models import PriceHistory
from . import commentary as cm
from .classify import classify
from .learn import grade_features
from .lifecycle import OPEN_STATES, Idea, Obs, manual_exit, step, summary_stats
from .parser import parse_many
from .store import ExtSignalStore

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
PLAY_PATTERN = {"Momentum Breakout": "Breakout", "Trend Pullback (buy the dip)": "Pullback",
                "Oversold Mean Reversion": "Mean reversion", "Overextended / Fade": "Mean reversion",
                "Bearish Breakdown": "Breakdown"}


@dataclass
class Message:
    ts: float
    text: str
    author: str | None = None
    source: str = "backtest"
    id: str | None = None


# --------------------------------------------------------------------------- loading
def _ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e11 else 1)
    v = str(v).strip()
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        d = datetime.strptime(v, "%m/%d/%Y")
    if d.tzinfo is None:
        d = d.replace(tzinfo=NY) if (d.hour or d.minute) else d.replace(hour=17, tzinfo=NY)   # date only: after the close
    return d.timestamp()


def load_messages(path: str | Path) -> list[Message]:
    """.json [{ts|date|timestamp, text|content, author}], .csv (same columns) or .txt with header lines
    ``### 2026-08-12 [14:30] [author]`` before each message."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    out: list[Message] = []
    if p.suffix.lower() == ".json":
        for i, r in enumerate(json.loads(raw)):
            out.append(Message(_ts(r.get("ts") or r.get("timestamp") or r.get("date")), r.get("text") or r.get("content") or "",
                               r.get("author"), id=str(r.get("id") or i)))
    elif p.suffix.lower() == ".csv":
        for i, r in enumerate(csv.DictReader(raw.splitlines())):
            out.append(Message(_ts(r.get("ts") or r.get("timestamp") or r.get("date")), r.get("text") or r.get("content") or "",
                               r.get("author") or None, id=str(i)))
    else:
        hdr = re.compile(r"^\s*(?:#{1,3}|@)\s*(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?\s*(.*)$")
        cur: Message | None = None
        buf: list[str] = []
        for line in raw.splitlines():
            m = hdr.match(line)
            if m:
                if cur is not None:
                    cur.text = "\n".join(buf).strip()
                    out.append(cur)
                ts = _ts(f"{m.group(1)}T{m.group(2)}" if m.group(2) else m.group(1))
                cur, buf = Message(ts, "", m.group(3).strip() or None, id=str(len(out))), []
            elif cur is not None:
                buf.append(line)
        if cur is not None:
            cur.text = "\n".join(buf).strip()
            out.append(cur)
        if not out:
            raise ValueError("no dated messages found: put a line like '### 2026-08-12 14:30 author' before each one")
    return sorted([m for m in out if m.text.strip()], key=lambda m: m.ts)


async def fetch_discord_history(settings, http, channel_id: str, limit: int = 500) -> list[Message]:
    from .discord import API, message_text
    h = {"Authorization": f"Bot {settings.discord_bot_token}"}
    out, before = [], None
    while len(out) < limit:
        params = {"limit": min(100, limit - len(out))}
        if before:
            params["before"] = before
        batch = await http.get_json(f"{API}/channels/{channel_id}/messages", provider="discord", params=params, headers=h)
        if not batch:
            break
        for m in batch:
            txt = message_text(m)
            if txt:
                out.append(Message(_ts(m["timestamp"]), txt, (m.get("author") or {}).get("username"), "discord", m["id"]))
        before = batch[-1]["id"]
        if len(batch) < params["limit"]:
            break
    return sorted(out, key=lambda m: m.ts)


# --------------------------------------------------------------------------- backtester
class Backtester:
    def __init__(self, engine, settings=None, gate: str | None = None, sim_paths: int = 800):
        from .tracker import ExtSignalTracker
        self.engine = engine
        self.s = settings or engine.settings
        self.gate = (gate if gate is not None else self.s.ext_min_entry_grade) or "none"
        self.tr = ExtSignalTracker(engine, ExtSignalStore(":memory:"), None, None, self.s)
        self.hist: dict[str, pd.DataFrame] = {}
        self.market: dict[str, pd.Series | None] = {}
        self.sim_paths = sim_paths

    async def _history(self, sym: str) -> pd.DataFrame | None:
        if sym not in self.hist:
            try:
                self.hist[sym] = (await self.engine.history(sym, "10y", "1d", ttl=86_400)).value.df
            except ABGError as e:
                log.warning("backtest: no history for %s: %s", sym, e.message)
                self.hist[sym] = None
        return self.hist[sym]

    async def _market(self) -> None:
        for s in ("SPY", "QQQ", "^VIX", "^TNX"):
            if s not in self.market:
                df = await self._history(s)
                self.market[s] = df["close"] if df is not None else None

    def _regime_at(self, d: date) -> dict | None:
        cut = {k: (v[v.index.date <= d] if v is not None else None) for k, v in self.market.items()}
        try:
            return regime_from_series(cut.get("SPY"), cut.get("QQQ"), cut.get("^VIX"), cut.get("^TNX"))
        except Exception:
            return None

    async def _context_at(self, sym: str, df: pd.DataFrame, upto: date) -> dict | None:
        cut = df[df.index.date <= upto]
        if len(cut) < 220:
            return None
        ph = PriceHistory.from_frame(sym, cut, "backtest", "1d")
        rep = await self.engine.analyze_history(ph, AnalyzeOptions(news=False, options=False, fundamentals=False,
                                                                    benchmark=False, ai=False, forecast=True,
                                                                    forecast_paths=self.sim_paths))
        return {"ts": time.time(), "report": rep, "close": cut["close"], "ohlc": cut}

    async def run(self, messages: list[Message], progress=None) -> dict:
        await self._market()
        syms = set()
        parsed: list[tuple[Message, list]] = []
        for m in messages:
            ps = parse_many(m.text)
            parsed.append((m, ps))
            syms |= {p.symbol for p in ps if p.symbol}
        for sym in syms:
            await self._history(sym)
        if not messages:
            raise ValueError("no messages to backtest")
        start = datetime.fromtimestamp(messages[0].ts, NY).date()
        days = sorted({d for df in self.hist.values() if df is not None for d in df.index.date if d >= start})
        by_day: dict[date, list] = {}
        for m, ps in parsed:
            dt = datetime.fromtimestamp(m.ts, NY)
            known = dt.date() if dt.hour >= 16 else dt.date() - timedelta(days=1)   # data known at message time
            by_day.setdefault(dt.date(), []).append((m, ps, known))
        ideas: list[Idea] = []
        rows: list[dict] = []
        skipped: list[dict] = []
        n_msgs = 0
        for d in days + [None]:
            for idea in [i for i in ideas if i.status in OPEN_STATES]:
                df = self.hist.get(idea.symbol)
                if d is None or df is None or idea.flags.get("start_day", "9999") > d.isoformat():
                    continue
                bar = df[df.index.date == d]
                if bar.empty:
                    continue
                b = bar.iloc[-1]
                ts = datetime(d.year, d.month, d.day, 16, tzinfo=NY).timestamp()
                was_pending = idea.status == "pending"
                step(idea, Obs(ts, float(b["close"]), float(b["high"]), float(b["low"]), float(b["open"]), bar=True),
                     approach_pct=self.s.ext_approach_pct, move_stop_to_be=self.s.ext_move_stop_to_breakeven,
                     allow_entry=cm.grade_ok(idea.grade, self.gate))
                if was_pending and idea.entry_at and idea.max_hold_until is None:
                    idea.max_hold_until = idea.entry_at + idea.flags.get("max_hold_days", 90) * 86400
            if d is None:
                break
            for m, ps, known in by_day.pop(d, []):
                n_msgs += 1
                for p in ps:
                    if p.kind == "update":
                        self._apply_update(p, m, d, ideas)
                    elif p.kind == "idea" and p.trackable:
                        idea = await self._new_idea(p, m, known, d, len(ideas) + 1)
                        if isinstance(idea, Idea):
                            ideas.append(idea)
                        else:
                            skipped.append({"message": m.text[:120], "reason": idea})
                if progress:
                    progress(n_msgs, len(messages))
        for i in ideas:
            rows.append(self._row(i))
        return self.report(rows, skipped, len(messages))

    async def _new_idea(self, p, m: Message, known: date, d: date, iid: int):
        df = self.hist.get(p.symbol)
        if df is None:
            return "no price history"
        ctx = await self._context_at(p.symbol, df, known)
        if ctx is None:
            return "not enough history before the signal"
        price = float(ctx["close"].iloc[-1])
        idea = Idea(symbol=p.symbol, direction=p.direction, entry_type=p.entry_type, entry_low=p.entry_low,
                    entry_high=p.entry_high, stop=p.stop, targets=list(p.targets), stop_basis=p.stop_basis,
                    instrument=p.instrument, timeframe=p.timeframe, source=m.source, author=m.author,
                    message_id=m.id, raw=p.raw[:2000], parse_confidence=p.confidence, soft_stop=p.soft_stop,
                    meta=dict(p.meta), id=iid, created_at=m.ts)
        reject = self.tr._prepare(idea, p, price, ctx)
        if reject:
            return reject
        now = time.time()
        idea.expires_at = m.ts + (idea.expires_at - now)
        idea.flags["start_day"] = (d + timedelta(days=1)).isoformat()
        idea.last_price = price
        b = await self.tr.barrier(idea, ctx, price)
        f = cm.facts(idea, ctx["report"], b, price, self._regime_at(known))
        idea.grade, idea.grade_score, idea.grade_reasons = cm.grade(idea, f)
        idea.meta["features"] = grade_features(idea, f, price)
        return idea

    def _apply_update(self, p, m: Message, d: date, ideas: list[Idea]) -> None:
        cands = [i for i in ideas if i.status in OPEN_STATES and (not p.symbol or i.symbol == p.symbol)
                 and (i.author == m.author)]
        if not cands:
            return
        idea = cands[-1]
        df = self.hist.get(idea.symbol)
        bar = df[df.index.date == d] if df is not None else None
        px = float(bar["close"].iloc[-1]) if bar is not None and not bar.empty else idea.last_price
        ts = datetime(d.year, d.month, d.day, 16, tzinfo=NY).timestamp()
        a = p.action
        if a in ("cancel", "close", "stop_hit"):
            if idea.status == "pending":
                idea.status, idea.closed_at, idea.close_reason = "cancelled", ts, f"source: {a}"
            elif px:
                manual_exit(idea, px, ts, reason=f"source: {a}")
        elif a == "trim" and idea.status == "active" and px:
            manual_exit(idea, px, ts, fraction=(p.fraction or 0.5) * idea.remaining, reason="source: trim")
        elif a in ("breakeven",) and idea.status == "active":
            idea.stop = idea.entry_price
        elif a == "move_stop" and p.new_stop:
            idea.stop = p.new_stop
        elif a == "move_entry" and p.new_entry and idea.status == "pending":
            idea.entry_low, idea.entry_high = min(p.new_entry), max(p.new_entry)

    @staticmethod
    def _row(i: Idea) -> dict:
        cls = (i.meta or {}).get("class") or {}
        return {"id": i.id, "ts": i.created_at, "date": datetime.fromtimestamp(i.created_at, NY).date().isoformat(),
                "symbol": i.symbol, "direction": i.direction, "author": i.author, "pattern": cls.get("pattern"),
                "basis": cls.get("basis"), "grade": i.grade, "grade_score": i.grade_score, "status": i.status,
                "close_reason": i.close_reason, "filled": i.entry_price is not None, "entry": i.entry_price,
                "r": round(i.realized_r, 4) if i.entry_price is not None else None,
                "open_r": round(i.open_r(i.last_price), 4) if i.status == "active" and i.last_price else None,
                "pnl": round(i.realized_pnl, 2), "targets_hit": len(i.targets_hit), "mfe_pct": round(i.mfe_pct, 2),
                "mae_pct": round(i.mae_pct, 2),
                "days_to_fill": round((i.entry_at - i.created_at) / 86400, 1) if i.entry_at else None,
                "days_held": round(((i.closed_at or time.time()) - i.entry_at) / 86400, 1) if i.entry_at else None,
                "features": (i.meta or {}).get("features")}

    @staticmethod
    def report(rows: list[dict], skipped: list[dict], n_messages: int) -> dict:
        closed = [r for r in rows if r["status"] == "closed" and r["filled"]]

        def agg(rs):
            rr = [r["r"] for r in rs if r["r"] is not None]
            w = [x for x in rr if x > 0.05]
            return {"trades": len(rs), "closed": len(rr), "win_rate": len(w) / len(rr) if rr else None,
                    "avg_r": sum(rr) / len(rr) if rr else None, "total_r": sum(rr)}
        groups = {}
        for key in ("author", "pattern", "grade", "basis"):
            g: dict = {}
            for r in closed:
                g.setdefault(r[key] or "n/a", []).append(r)
            groups[key] = {k: agg(v) for k, v in sorted(g.items())}
        curve, cum = [], 0.0
        for r in sorted(closed, key=lambda r: r["ts"]):
            cum += r["r"]
            curve.append({"date": r["date"], "cum_r": round(cum, 3)})
        dd = 0.0
        peak = 0.0
        for c in curve:
            peak = max(peak, c["cum_r"])
            dd = min(dd, c["cum_r"] - peak)
        filled = [r for r in rows if r["filled"]]
        overall = {**agg(closed), "ideas": len(rows), "messages": n_messages, "skipped": len(skipped),
                   "fill_rate": len(filled) / len(rows) if rows else None,
                   "never_filled": sum(r["status"] in ("missed", "invalidated", "expired") for r in rows),
                   "max_drawdown_r": round(dd, 3),
                   "avg_days_held": (sum(r["days_held"] or 0 for r in closed) / len(closed)) if closed else None}
        calib = groups["grade"]
        gate_note = None
        if calib.get("D", {}).get("closed") and any(calib.get(g, {}).get("closed") for g in "ABC"):
            good = [calib[g] for g in "ABC" if calib.get(g, {}).get("closed")]
            avg_good = sum(x["total_r"] for x in good) / max(1, sum(x["closed"] for x in good))
            gate_note = (f"D-graded trades averaged {calib['D']['avg_r']:+.2f}R vs {avg_good:+.2f}R for A-C: the grade gate "
                         + ("helps." if calib["D"]["avg_r"] < avg_good else "did not help on this sample."))
        return {"overall": overall, "by": groups, "equity_curve": curve, "grade_note": gate_note,
                "trades": rows, "skipped": skipped[:200], "generated_at": time.time()}


def save_report(rep: dict, data_dir, name: str) -> dict:
    d = Path(data_dir).expanduser() / "backtests"
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{re.sub(r'[^A-Za-z0-9_-]+', '-', name)[:40]}-{datetime.now(NY):%Y%m%d-%H%M}"
    (d / f"{stem}.json").write_text(json.dumps(rep, default=str, indent=1))
    with open(d / f"{stem}.csv", "w", newline="") as fh:
        cols = [k for k in rep["trades"][0].keys() if k != "features"] if rep["trades"] else ["id"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rep["trades"])
    return {"json": str(d / f"{stem}.json"), "csv": str(d / f"{stem}.csv")}


# --------------------------------------------------------------------------- training data from setups
async def generate_setups(engine, symbols: list[str], years: int = 8, every: int = 5, hold_days: int = 40,
                          min_confidence: float = 0.75, progress=None) -> list[dict]:
    """Labelled trades from the terminal's own setup detector (no look-ahead: each setup uses bars up to t)."""
    bt = Backtester(engine)
    await bt._market()
    rows: list[dict] = []
    for k, sym in enumerate(symbols):
        try:
            df = (await engine.history(sym, f"{years}y", "1d", ttl=86_400)).value.df
        except ABGError as e:
            log.warning("setups: %s skipped: %s", sym, e.message)
            continue
        ind = ta.compute_all(df)
        n = len(ind)
        for t in range(260, n - 5, every):
            sl = ind.iloc[: t + 1]
            plays = [p for p in classify_plays(sl, min_confidence) if p["direction"] in ("long", "short") and p["levels"]]
            if not plays:
                continue
            p = plays[0]
            lv = p["levels"]
            d0 = sl.index[-1]
            c0 = float(sl["close"].iloc[-1])
            idea = Idea(symbol=sym, direction=p["direction"], entry_type="market", entry_low=c0, entry_high=c0,
                        stop=lv["stop"], targets=[lv["target_1"], lv["target_2"]], id=len(rows) + 1,
                        created_at=pd.Timestamp(d0).tz_localize(NY).timestamp() if pd.Timestamp(d0).tzinfo is None
                        else pd.Timestamp(d0).timestamp())
            idea.stop_initial = idea.stop
            idea.meta["class"] = {"pattern": PLAY_PATTERN.get(p["name"], "Discretionary"), "basis": "Technical"}
            rep = {"indicators": ta.latest_snapshot(sl), "signal": composite_signal(sl), "regime": ta_regime(sl),
                   "levels": {}}
            f = cm.facts(idea, rep, None, c0, bt._regime_at(pd.Timestamp(d0).date()))
            feats = grade_features(idea, f, c0)
            idea.shares, idea.flags["multiplier"] = 1.0, 1.0
            fut = ind.iloc[t + 1: t + 1 + hold_days]
            for ts, b in fut.iterrows():
                bts = pd.Timestamp(ts).timestamp()
                step(idea, Obs(bts, float(b["close"]), float(b["high"]), float(b["low"]), float(b["open"]), bar=True))
                if idea.status not in OPEN_STATES:
                    break
            if idea.status == "active":                      # time exit at the end of the window
                manual_exit(idea, float(fut["close"].iloc[-1]), time.time(), reason="window end")
            if idea.entry_price is None:
                continue
            rows.append({"ts": idea.created_at, "symbol": sym, "setup": p["name"], "direction": idea.direction,
                         "r": idea.realized_r, "features": feats, "source": "setups"})
        if progress:
            progress(k + 1, len(symbols), sym, len(rows))
    return rows


def summary_of(ideas: list[Idea]) -> dict:
    return summary_stats(ideas)


def classify_text(p) -> dict:
    return classify(p.raw, setup=p.meta.get("setup"), direction=p.direction, entry_type=p.entry_type,
                    entry_low=p.entry_low, entry_high=p.entry_high, stop=p.stop, timeframe=p.timeframe)

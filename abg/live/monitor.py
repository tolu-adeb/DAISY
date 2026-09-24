"""The live monitor: continuously watches the saved portfolio and emits signals.

Two sweeps run on independent timers:

* **quote sweep** (every ``monitor_quote_interval``, default 60 s while the market is open):
  live quotes for every tracked symbol → big-move bands, support/resistance breaks, stop /
  target hits, position-loss bands, custom price rules, portfolio day-move and concentration.
* **analysis sweep** (every ``monitor_analysis_interval``, default 15 min): full analysis per
  symbol with the live quote spliced into today's daily bar, so RSI/MACD/setups/risk reflect
  the current session → signal-label changes, new setups, RSI zones, MACD/MA crosses, risk
  level changes, sentiment shifts, RSI/score rules.

API budget: daily history is reused for ``monitor_history_ttl`` (6 h) and only the live quote
changes between sweeps, so a 20-symbol portfolio costs ~20 quote calls/min (Finnhub free = 60/min)
plus a handful of news calls per analysis sweep.

Outside regular NYSE hours the quote sweep slows to ``monitor_offhours_interval``.
Only one monitor per data directory runs at a time (heartbeat lock file), so running
``abg serve`` and ``abg monitor`` together never double-sends alerts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections import deque
from dataclasses import fields
from pathlib import Path

from ..engine import AnalysisEngine, AnalyzeOptions
from ..errors import ABGError
from ..models import Quote
from ..portfolio.store import PortfolioStore
from ..portfolio.valuation import fetch_quotes, snapshot
from ..utils import jsonable
from .market_hours import is_market_open, session_status
from .notify import NotificationHub
from .signals import SignalEngine

log = logging.getLogger(__name__)
_QUOTE_FIELDS = {f.name for f in fields(Quote)}


class MonitorBusy(ABGError):
    code = "monitor_busy"


class MonitorLock:
    def __init__(self, path: Path, stale_after: float = 180.0):
        self.path = Path(path)
        self.stale_after = stale_after
        self.me = {"pid": os.getpid(), "host": socket.gethostname()}

    def holder(self) -> dict | None:
        try:
            d = json.loads(self.path.read_text())
        except Exception:
            return None
        if time.time() - d.get("heartbeat", 0) > self.stale_after:
            return None
        return d

    def acquire(self) -> bool:
        h = self.holder()
        if h and (h.get("pid"), h.get("host")) != (self.me["pid"], self.me["host"]):
            return False
        self.beat(started=True)
        return True

    def beat(self, started: bool = False) -> None:
        d = {**self.me, "heartbeat": time.time()}
        if started:
            d["started"] = time.time()
        else:
            try:
                d["started"] = json.loads(self.path.read_text()).get("started", time.time())
            except Exception:
                d["started"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.path)

    def release(self) -> None:
        h = self.holder()
        if h and (h.get("pid"), h.get("host")) == (self.me["pid"], self.me["host"]):
            try:
                self.path.unlink()
            except OSError:
                pass


def _quote_obj(d: dict) -> Quote | None:
    try:
        return Quote(**{k: v for k, v in d.items() if k in _QUOTE_FIELDS})
    except Exception:
        return None


class Monitor:
    def __init__(self, engine: AnalysisEngine, store: PortfolioStore, hub: NotificationHub,
                 portfolio: str | None = None):
        self.engine, self.store, self.hub = engine, store, hub
        self.s = engine.settings
        self.pf = store.ensure(portfolio or self.s.default_portfolio)
        self.signals = SignalEngine(store, self.s, self.pf)
        self.lock = MonitorLock(Path(self.s.data_dir).expanduser() / "monitor.lock",
                                stale_after=max(180.0, 3 * self.s.monitor_quote_interval))
        self.quotes: dict[str, dict] = {}
        self.analysis: dict[str, dict] = {}
        self.levels: dict[str, dict] = {}
        self.last_snapshot: dict | None = None
        self.errors: deque = deque(maxlen=25)
        self.running = False
        self.started_at: float | None = None
        self.last_quote_sweep: float | None = None
        self.last_analysis_sweep: float | None = None
        self.next_quote_at = 0.0
        self.next_analysis_at = 0.0
        self.sweeps = 0
        self.signals_emitted = 0
        self._poked = False
        self._poked_analysis = False
        self._failed: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ control
    def poke(self, analysis: bool = False) -> None:
        """Portfolio changed (or user asked for a refresh): sweep now.  Pokes that arrive while
        a sweep is running are remembered, so a burst of edits is never lost."""
        self.next_quote_at = 0.0
        self._poked = True
        if analysis:
            self.next_analysis_at = 0.0
            self._poked_analysis = True
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        if not self.lock.acquire():
            h = self.lock.holder() or {}
            raise MonitorBusy(f"another monitor is already running (pid {h.get('pid')} on {h.get('host')}); "
                              f"signals are still recorded there")
        self.running, self.started_at = True, time.time()
        self._stop.clear()
        log.info("monitor started for portfolio %s", self.pf)
        was_open = None
        try:
            while not self._stop.is_set():
                open_ = is_market_open() or not self.s.monitor_market_hours_only
                if was_open is not None and open_ != was_open:
                    self.next_quote_at = self.next_analysis_at = 0.0     # session boundary: refresh everything
                was_open = open_
                q_int = self.s.monitor_quote_interval if open_ else self.s.monitor_offhours_interval
                a_int = self.s.monitor_analysis_interval if open_ else max(self.s.monitor_offhours_interval * 4,
                                                                            self.s.monitor_analysis_interval)
                now = time.monotonic()
                if now >= self.next_analysis_at:
                    self._poked = self._poked_analysis = False
                    await self._guard("analysis sweep", self.analysis_sweep())
                    self.next_analysis_at = 0.0 if self._poked_analysis else time.monotonic() + a_int
                    self.next_quote_at = 0.0 if self._poked else time.monotonic() + q_int
                elif now >= self.next_quote_at:
                    self._poked = False
                    await self._guard("quote sweep", self.quote_sweep())
                    self.next_quote_at = 0.0 if self._poked else time.monotonic() + q_int
                try:
                    self.lock.beat()
                except OSError as e:
                    self.errors.append({"ts": time.time(), "where": "lock", "error": str(e)})
                wait = max(1.0, min(self.next_quote_at, self.next_analysis_at) - time.monotonic())
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.running = False
            self.lock.release()
            log.info("monitor stopped")

    async def _guard(self, where: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:           # a bad sweep must never kill the monitor
            log.exception("%s failed", where)
            self.errors.append({"ts": time.time(), "where": where, "error": f"{type(e).__name__}: {e}"[:300]})

    # ------------------------------------------------------------------ sweeps
    async def quote_sweep(self) -> list:
        syms = self.store.symbols(self.pf)
        if not syms:
            self.last_quote_sweep = time.time()
            return []
        cutoff = time.time() - self.s.monitor_analysis_interval
        new = [x for x in syms if x not in self.analysis and self._failed.get(x, 0) < cutoff]
        if new:                                   # symbols added since the last analysis sweep
            return await self.analysis_sweep(new)
        quotes = await fetch_quotes(self.engine, syms, use_cache=False)
        return await self._process_quotes(quotes)

    async def analysis_sweep(self, only: list[str] | None = None) -> list:
        tracked = self.store.symbols(self.pf)
        for gone in set(self.analysis) - set(tracked):        # forget removed symbols
            self.analysis.pop(gone, None)
            self.levels.pop(gone, None)
        syms = [x for x in tracked if only is None or x in only]
        if not syms:
            self.last_analysis_sweep = self.last_quote_sweep = time.time()
            return []
        quotes = await fetch_quotes(self.engine, syms, use_cache=False)
        sem = asyncio.Semaphore(3)
        emitted: list = []

        async def one(sym: str):
            async with sem:
                q = _quote_obj(quotes.get(sym) or {})
                try:
                    r = await self.engine.analyze(sym, AnalyzeOptions(
                        period="1y", ai=False, options=False, news=True, fundamentals=True, benchmark=True,
                        forecast_paths=2000,
                        live_quote=q, history_ttl=self.s.monitor_history_ttl))
                except ABGError as e:
                    self._failed[sym] = time.time()
                    self.errors.append({"ts": time.time(), "where": f"analyze {sym}", "error": e.message[:300]})
                    return
                self._failed.pop(sym, None)
                risk = next((x for x in r.get("risk") or [] if "error" not in x), {})
                plays = [p for p in r.get("plays") or [] if p.get("name") != "No Clear Setup"]
                self.analysis[sym] = {
                    "signal_score": r["signal"]["score"], "signal_label": r["signal"]["label"],
                    "trend": r["regime"]["trend"], "rsi": r["indicators"].get("rsi_14"),
                    "risk_level": risk.get("level"), "risk_score": risk.get("score"),
                    "top_setup": plays[0]["name"] if plays else None, "analyzed_at": time.time(),
                    "sentiment": (r.get("sentiment") or {}).get("score"), "live_bar": r["data_quality"].get("live_bar"),
                    "recommendation": ((r.get("forecast") or {}).get("recommendation") or {}).get("action"),
                    "forecast_confidence": ((r.get("forecast") or {}).get("confidence") or {}).get("rating"),
                    "prob_up": ((r.get("forecast") or {}).get("recommendation") or {}).get("prob_up")}
                self.levels[sym] = r.get("levels") or {}
                for sig in self.signals.from_report(sym, r):
                    emitted.append(await self._emit(sig))

        await asyncio.gather(*(one(s) for s in syms))
        self.last_analysis_sweep = time.time()
        if only is not None:                      # partial sweep: price the rest of the book too
            rest = [x for x in tracked if x not in quotes]
            if rest:
                quotes = {**quotes, **await fetch_quotes(self.engine, rest, use_cache=False)}
        emitted += await self._process_quotes(quotes)
        return emitted

    async def _process_quotes(self, quotes: dict[str, dict]) -> list:
        positions = {p.symbol: p for p in self.store.positions(self.pf)}
        emitted = []
        for sym, q in quotes.items():
            if q.get("error"):
                self.errors.append({"ts": time.time(), "where": f"quote {sym}", "error": q["error"][:300]})
                continue
            for sig in self.signals.from_quote(sym, q, self.levels.get(sym), positions.get(sym)):
                emitted.append(await self._emit(sig))
        self.quotes = quotes
        snap = await snapshot(self.engine, self.store, self.pf, quotes=quotes, analysis=self.analysis, with_risk=False)
        for sig in self.signals.from_snapshot(snap):
            emitted.append(await self._emit(sig))
        self.last_snapshot = snap
        self.last_quote_sweep = time.time()
        self.sweeps += 1
        self.hub.broadcast("snapshot", snap)
        self.hub.broadcast("status", self.status())
        return emitted

    async def _emit(self, sig):
        self.signals_emitted += 1
        return await self.hub.publish(sig)

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        mono, wall = time.monotonic(), time.time()
        in_ = lambda t: None if not self.running or not t else max(0.0, t - mono)  # noqa: E731
        holder = self.lock.holder()
        return jsonable({
            "running": self.running, "portfolio": self.pf, "started_at": self.started_at,
            "elsewhere": (holder if (holder and not self.running) else None),
            "symbols": self.store.symbols(self.pf), "market": session_status(),
            "last_quote_sweep": self.last_quote_sweep, "last_analysis_sweep": self.last_analysis_sweep,
            "next_quote_in_s": in_(self.next_quote_at), "next_analysis_in_s": in_(self.next_analysis_at),
            "sweeps": self.sweeps, "signals_emitted": self.signals_emitted, "errors": list(self.errors)[-10:],
            "channels": self.hub.describe(), "now": wall,
            "intervals": {"quote_s": self.s.monitor_quote_interval, "analysis_s": self.s.monitor_analysis_interval,
                          "offhours_quote_s": self.s.monitor_offhours_interval}})

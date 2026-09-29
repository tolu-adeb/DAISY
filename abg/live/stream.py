"""Real-time prices: Finnhub websocket trade stream.

    wss://ws.finnhub.io?token=KEY   {"type":"subscribe","symbol":"AAPL"}  ->  {"type":"trade","data":[{"s","p","t","v"}]}

Trades are buffered per symbol (last, high, low, count since the last flush) and handed to the
tracker every ``ABG_EXT_STREAM_FLUSH_SECONDS`` (1 s), so a spike through a stop between two
flushes is still seen (the high/low travel with the batch).  Finnhub's free plan streams US
stocks/ETFs, forex (OANDA:EUR_USD) and crypto (BINANCE:BTCUSDT); futures, yields and indexes are
polled separately by the monitor (``ABG_EXT_FAST_POLL_SECONDS``).

Robust by design: automatic reconnect with backoff (1 s -> 60 s), re-subscribing on reconnect and
when the tracked symbol set changes, and a watchdog that reconnects when no message arrives for
90 s during market hours.  It never raises into the monitor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from ..markets.instruments import spec_for

log = logging.getLogger(__name__)
FINNHUB_WS = "wss://ws.finnhub.io"


def stream_symbol(sym: str) -> str | None:
    """Our symbol -> Finnhub stream symbol (None if the free stream can't serve it)."""
    sp = spec_for(sym)
    if sp.asset_class in ("stock", "etf", "bond_etf"):
        return sym.replace("-", ".") if "-" in sym and not sym.endswith("-USD") else sym
    if sp.asset_class == "crypto" and sym.endswith("-USD"):
        return f"BINANCE:{sym[:-4]}USDT"
    if sp.asset_class == "fx" and sym.endswith("=X") and len(sym) == 8:
        return f"OANDA:{sym[:3]}_{sym[3:6]}"
    return None


class PriceStream:
    name = "finnhub_stream"

    def __init__(self, api_key: str, on_flush: Callable[[dict[str, dict]], Awaitable[None]],
                 flush_seconds: float = 1.0, url: str | None = None, idle_timeout: float = 90.0):
        self.key = api_key
        self.url = url or f"{FINNHUB_WS}?token={api_key}"
        self.on_flush = on_flush
        self.flush_seconds = flush_seconds
        self.idle_timeout = idle_timeout
        self.wanted: dict[str, str] = {}          # stream symbol -> our symbol
        self.subscribed: set[str] = set()
        self.buf: dict[str, dict] = {}
        self.last: dict[str, dict] = {}
        self.connected = False
        self.running = False
        self.ticks = 0
        self.reconnects = 0
        self.last_msg_at: float | None = None
        self.errors: deque = deque(maxlen=10)
        self._ws = None
        self._stop = asyncio.Event()
        self._changed = asyncio.Event()

    # ------------------------------------------------------------------ symbols
    def set_symbols(self, symbols) -> list[str]:
        """Track these of our symbols; returns the ones the stream can't serve (poll those)."""
        want, rest = {}, []
        for s in set(symbols):
            fs = stream_symbol(s)
            if fs:
                want[fs] = s
            else:
                rest.append(s)
        if want != self.wanted:
            self.wanted = want
            self._changed.set()
        return sorted(rest)

    def streamed(self) -> set[str]:
        return set(self.wanted.values()) if self.connected else set()

    async def _sync_subscriptions(self) -> None:
        ws = self._ws
        if ws is None:
            return
        want = set(self.wanted)
        for s in sorted(want - self.subscribed):
            await ws.send(json.dumps({"type": "subscribe", "symbol": s}))
        for s in sorted(self.subscribed - want):
            await ws.send(json.dumps({"type": "unsubscribe", "symbol": s}))
        self.subscribed = want

    # ------------------------------------------------------------------ run
    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        import websockets
        self.running = True
        self._stop.clear()
        backoff = 1.0
        flusher = asyncio.ensure_future(self._flush_loop())
        try:
            while not self._stop.is_set():
                if not self.wanted:
                    self._changed.clear()
                    await self._wait_any(self._changed.wait(), 30)
                    continue
                try:
                    async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, open_timeout=15,
                                                  max_size=2 ** 22) as ws:
                        self._ws, self.connected, self.subscribed = ws, True, set()
                        self.last_msg_at = time.time()
                        await self._sync_subscriptions()
                        backoff = 1.0
                        await self._recv_loop(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.errors.append({"ts": time.time(), "error": f"{type(e).__name__}: {str(e)[:160]}"})
                    log.info("price stream disconnected: %s", e)
                finally:
                    self._ws, self.connected = None, False
                if self._stop.is_set():
                    break
                self.reconnects += 1
                await self._wait_any(self._stop.wait(), backoff)
                backoff = min(backoff * 2, 60.0)
        finally:
            flusher.cancel()
            self.running = False

    async def _recv_loop(self, ws) -> None:
        while not self._stop.is_set():
            if self._changed.is_set():
                self._changed.clear()
                await self._sync_subscriptions()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                if self.last_msg_at and time.time() - self.last_msg_at > self.idle_timeout and self._busy_hours():
                    raise ConnectionError(f"no data for {self.idle_timeout:.0f}s")
                continue
            self.last_msg_at = time.time()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "ping":
                continue
            if msg.get("type") == "error":
                self.errors.append({"ts": time.time(), "error": str(msg.get("msg"))[:200]})
                continue
            if msg.get("type") != "trade":
                continue
            for t in msg.get("data") or []:
                ours = self.wanted.get(t.get("s"))
                p = t.get("p")
                if ours is None or not isinstance(p, (int, float)) or p <= 0:
                    continue
                b = self.buf.get(ours)
                ts = (t.get("t") or time.time() * 1000) / 1000
                if b is None:
                    self.buf[ours] = {"price": p, "tick_high": p, "tick_low": p, "ts": ts, "n": 1, "source": "stream"}
                else:
                    b["price"], b["ts"], b["n"] = p, ts, b["n"] + 1
                    b["tick_high"], b["tick_low"] = max(b["tick_high"], p), min(b["tick_low"], p)
                self.ticks += 1

    def _busy_hours(self) -> bool:
        from ..live.market_hours import is_market_open
        return is_market_open() or any(k.startswith("BINANCE:") for k in self.wanted)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_seconds)
            if not self.buf:
                continue
            batch, self.buf = self.buf, {}
            self.last.update(batch)
            try:
                await self.on_flush(batch)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the tracker must never kill the stream
                log.exception("stream flush failed")
                self.errors.append({"ts": time.time(), "error": f"flush: {type(e).__name__}: {e}"[:200]})

    @staticmethod
    async def _wait_any(coro, timeout: float) -> None:
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            pass

    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "running": self.running,
                "symbols": sorted(self.wanted.values()), "ticks": self.ticks, "reconnects": self.reconnects,
                "last_message_at": self.last_msg_at, "errors": list(self.errors)[-3:]}
